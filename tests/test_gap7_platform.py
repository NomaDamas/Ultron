from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ultron.app.server import create_app
from ultron.app.triage import build_durable_triage_app_for_tests
from ultron.auth.principal import Principal, Scope
from ultron.evolution.variation import VariationPrimitive
from ultron.hermes.integrity import VendorIntegrityError, verify_vendor_integrity


def _csrf(client):
    response = client.get("/")
    return response.cookies["ultron_csrf"]


def _privileged(client, csrf, action_type, payload, pointer_version=None):
    version = client.app.state.triage.current_pointer_version() if pointer_version is None else pointer_version
    return client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={"type": action_type, "payload": payload, "csrf_token": csrf, "active_pointer_version": version},
    )


def test_auth_session_expiry_scope_gate_and_secure_cookie_attrs():
    client = TestClient(create_app())
    root = client.get("/")
    set_cookie = root.headers.get_list("set-cookie")
    assert any("ultron_session" in item and "HttpOnly" in item and "SameSite=strict" in item for item in set_cookie)
    csrf = root.cookies["ultron_csrf"]

    store = client.app.state.session_store
    expired_token = store.create_session(Principal(subject="expired", scopes=frozenset(scope.value for scope in Scope), tenant_scope="local"), 1, now=0)
    client.cookies.set("ultron_session", expired_token)
    client.cookies.set("ultron_csrf", csrf)
    expired = _privileged(client, csrf, "REQUEST_PERMISSION_EXPANSION", {"tool": "network"})
    assert expired.status_code == 401

    limited = Principal(subject="limited", scopes=frozenset(), tenant_scope="local")
    limited_token = store.create_session(limited, 60)
    limited_csrf = "limited-csrf"
    client.app.state.session_store._sessions[limited_token] = client.app.state.session_store._sessions[limited_token]
    client.cookies.set("ultron_session", limited_token)
    client.cookies.set("ultron_csrf", limited_csrf)
    # mirror the server csrf map by getting a normal session, then replace with limited token csrf via closure-unavailable HTTP path is not exposed;
    # no-scope check occurs before CSRF validation.
    denied = client.post(
        "/api/action",
        headers={"X-CSRF-Token": limited_csrf},
        json={"type": "REQUEST_PERMISSION_EXPANSION", "payload": {}, "csrf_token": limited_csrf, "active_pointer_version": client.app.state.triage.current_pointer_version()},
    )
    assert denied.status_code == 403
    assert "scope" in denied.json()["detail"]

    allowed = client.post("/api/action", json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "unprivileged still allowed"}})
    assert allowed.status_code == 200


def test_actor_audit_in_memory_and_durable_promote_restore(tmp_path):
    client = TestClient(create_app())
    csrf = _csrf(client)
    submitted = client.post("/api/action", json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "actor audit"}})
    candidate_hash = submitted.json()["candidate"]["content_hash"]
    canary_id = submitted.json()["canary_id"]
    assert _privileged(client, csrf, "ROLLBACK_CANARY", {"canary_id": canary_id}).status_code == 200
    run_entries = client.app.state.triage.ledger.entries_for_run(submitted.json()["result"]["run_manifest"]["run_id"])
    assert run_entries and {entry.actor for entry in run_entries} == {"local-operator"}
    assert submitted.json()["result"]["run_manifest"]["actor"] == "local-operator"

    app = build_durable_triage_app_for_tests(str(tmp_path / "durable.sqlite"))
    app.seed_baseline()
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "actor-durable"})
    h = canary["candidate"].content_hash
    app.benchmark_and_decide(h, canary_id=canary["canary_id"])
    app.approve_promotion(h, app.current_pointer_version(), actor="durable-actor")
    entries = [entry for entry in app.ledger.promotable_entries() if entry.payload.get("action") == "promote"]
    assert entries[-1].actor == "durable-actor"
    assert entries[-1].payload["actor"] == "durable-actor"


def test_metrics_counters_and_no_secret_fields():
    client = TestClient(create_app())
    csrf = _csrf(client)
    submitted = client.post("/api/action", json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "metrics"}})
    body = submitted.json()
    benchmarked = client.post("/api/action", json={"type": "RUN_BENCHMARK", "payload": {"candidate_hash": body["candidate"]["content_hash"], "canary_id": body["canary_id"]}})
    assert benchmarked.status_code == 200
    rolled = _privileged(client, csrf, "ROLLBACK_CANARY", {"canary_id": body["canary_id"]})
    assert rolled.status_code == 200
    metrics = client.get("/api/metrics").json()
    assert metrics["runs_started"] == 1
    assert metrics["benchmarks_run"] == 1
    assert metrics["rollbacks"] == 1
    assert "password" not in metrics and "token" not in metrics and "secret" not in metrics


def test_vendor_integrity_absent_and_fail_closed_on_corruption(tmp_path):
    absent = verify_vendor_integrity(tmp_path / "missing")
    assert absent.status == "vendor-absent"

    vendor = tmp_path / "vendor"
    files = {
        "toolsets.py": "toolsets",
        "agent/conversation_loop.py": "loop",
        "agent/iteration_budget.py": "budget",
        "agent/trajectory.py": "trajectory",
        "agent/prompt_builder.py": "prompt",
    }
    for rel, text in files.items():
        path = vendor / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    # The committed manifest is for the real vendored tree; a copied/corrupted tree must fail closed.
    with pytest.raises(VendorIntegrityError):
        verify_vendor_integrity(vendor)


def test_readme_mentions_real_vs_seam_and_gap_statuses():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "Real vs seam" in text
    assert "G001-G007" in text
    assert "GAP1-GAP7" in text
