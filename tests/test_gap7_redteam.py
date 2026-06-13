import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from ultron.app.server import create_app
from ultron.app.triage import build_durable_triage_app_for_tests
from ultron.auth.principal import Principal, Scope
from ultron.evolution.variation import VariationPrimitive
from ultron.hermes import integrity
from ultron.hermes.integrity import VendorIntegrityError
from ultron.ledger.side_effect_ledger import SideEffectKind
from ultron.registry.store import ModuleLifecycle
from ultron.ui.runtime import ActionType


def _action_payload(action_type, *, csrf=None, pointer_version=1, payload=None):
    return {
        "type": action_type.value,
        "payload": payload or {},
        "csrf_token": csrf,
        "active_pointer_version": pointer_version,
    }


def _privileged_state(app):
    engine = app.state.triage
    return {
        "pointer": engine.pointer_store.get(engine.pointer_key),
        "ledger": [entry.model_dump(mode="json") for entry in engine.ledger.promotable_entries()],
        "pending_permission_expansions": list(engine.pending_permission_expansions),
    }


def _login(client):
    response = client.get("/")
    assert response.status_code == 200
    session = client.cookies.get("ultron_session")
    csrf = client.cookies.get("ultron_csrf")
    assert session
    assert csrf
    return session, csrf


def test_privileged_action_rejections_preserve_pointer_and_state():
    app = create_app()
    client = TestClient(app)
    pointer_version = app.state.triage.current_pointer_version()
    command = _action_payload(ActionType.APPROVE_PROMOTION, pointer_version=pointer_version, payload={"candidate_hash": "missing"})

    before = _privileged_state(app)
    response = client.post("/api/action", json=command)
    assert response.status_code == 401
    assert _privileged_state(app) == before

    expired = app.state.session_store.create_session(
        Principal(subject="expired-operator", scopes=frozenset({Scope.APPROVE_PROMOTION.value})),
        ttl=1,
        now=0,
    )
    before = _privileged_state(app)
    response = client.post("/api/action", json=command, cookies={"ultron_session": expired})
    assert response.status_code == 401
    assert _privileged_state(app) == before

    no_scope = app.state.session_store.create_session(Principal(subject="scope-less", scopes=frozenset()), ttl=60)
    before = _privileged_state(app)
    response = client.post("/api/action", json=command, cookies={"ultron_session": no_scope})
    assert response.status_code == 403
    assert "approve_promotion" in response.json()["detail"]
    assert _privileged_state(app) == before

    scoped, csrf = _login(client)
    before = _privileged_state(app)
    response = client.post("/api/action", json=command, cookies={"ultron_session": scoped})
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]
    assert _privileged_state(app) == before

    before = _privileged_state(app)
    stale = _action_payload(ActionType.APPROVE_PROMOTION, csrf=csrf, pointer_version=pointer_version - 1, payload={"candidate_hash": "missing"})
    response = client.post("/api/action", json=stale, cookies={"ultron_session": scoped}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 403
    assert "stale active pointer" in response.json()["detail"]
    assert _privileged_state(app) == before


@pytest.mark.parametrize(
    ("action_type", "scope", "payload"),
    [
        (ActionType.RUN_BENCHMARK, Scope.RUN_BENCHMARK, {"candidate_hash": "missing", "canary_id": "missing"}),
        (ActionType.ROLLBACK_CANARY, Scope.ROLLBACK, {"canary_id": "missing"}),
        (ActionType.RESTORE_MODULE, Scope.RESTORE, {"module_hash": "missing"}),
    ],
)
def test_other_privileged_actions_reject_auth_scope_csrf_and_stale_pointer(action_type, scope, payload):
    app = create_app()
    client = TestClient(app)
    pointer_version = app.state.triage.current_pointer_version()
    command = _action_payload(action_type, pointer_version=pointer_version, payload=payload)

    before = _privileged_state(app)
    assert client.post("/api/action", json=command).status_code == 401
    assert _privileged_state(app) == before

    expired = app.state.session_store.create_session(Principal(subject="expired", scopes=frozenset({scope.value})), ttl=1, now=0)
    before = _privileged_state(app)
    assert client.post("/api/action", json=command, cookies={"ultron_session": expired}).status_code == 401
    assert _privileged_state(app) == before

    wrong_scope = app.state.session_store.create_session(Principal(subject="wrong-scope", scopes=frozenset({Scope.APPROVE_PROMOTION.value})), ttl=60)
    before = _privileged_state(app)
    assert client.post("/api/action", json=command, cookies={"ultron_session": wrong_scope}).status_code == 403
    assert _privileged_state(app) == before

    session, csrf = _login(client)
    before = _privileged_state(app)
    assert client.post("/api/action", json=command, cookies={"ultron_session": session}).status_code == 403
    assert _privileged_state(app) == before

    stale = _action_payload(action_type, csrf=csrf, pointer_version=pointer_version - 1, payload=payload)
    before = _privileged_state(app)
    response = client.post("/api/action", json=stale, cookies={"ultron_session": session}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 403
    assert _privileged_state(app) == before


def test_index_session_cookie_is_httponly_and_samesite():
    response = TestClient(create_app()).get("/")
    set_cookies = response.headers.get_list("set-cookie")
    session_cookie = next(cookie for cookie in set_cookies if cookie.startswith("ultron_session="))
    assert "HttpOnly" in session_cookie
    assert "SameSite=strict" in session_cookie


def test_api_actor_audit_for_benchmark_promotion_and_rollback():
    app = create_app()
    client = TestClient(app)
    session, csrf = _login(client)
    engine = app.state.triage
    principal = app.state.session_store.resolve(session)
    assert principal is not None

    submit = client.post(
        "/api/action",
        json={"type": ActionType.SUBMIT_REQUEST.value, "payload": {"request_text": "gap7 actor audit"}},
        cookies={"ultron_session": session},
    )
    assert submit.status_code == 200
    candidate_hash = submit.json()["candidate"]["content_hash"]
    canary_id = submit.json()["canary_id"]

    benchmark = client.post(
        "/api/action",
        json={
            "type": ActionType.RUN_BENCHMARK.value,
            "payload": {"candidate_hash": candidate_hash, "canary_id": canary_id},
            "csrf_token": csrf,
            "active_pointer_version": engine.current_pointer_version(),
        },
        cookies={"ultron_session": session},
        headers={"X-CSRF-Token": csrf},
    )
    assert benchmark.status_code == 200

    promote = client.post(
        "/api/action",
        json=_action_payload(
            ActionType.APPROVE_PROMOTION,
            csrf=csrf,
            pointer_version=engine.current_pointer_version(),
            payload={"candidate_hash": candidate_hash},
        ),
        cookies={"ultron_session": session},
        headers={"X-CSRF-Token": csrf},
    )
    assert promote.status_code == 200

    rollback = client.post(
        "/api/action",
        json=_action_payload(ActionType.ROLLBACK_CANARY, csrf=csrf, pointer_version=engine.current_pointer_version(), payload={"canary_id": canary_id}),
        cookies={"ultron_session": session},
        headers={"X-CSRF-Token": csrf},
    )
    assert rollback.status_code == 200

    entries = engine.ledger.promotable_entries()
    assert engine.registry.get(candidate_hash).lifecycle is ModuleLifecycle.SURVIVOR
    assert candidate_hash not in engine.pointer_store.get(engine.pointer_key)[1]
    assert engine.telemetry.snapshot()["promotions"] == 1
    assert engine.telemetry.snapshot()["rollbacks"] == 1
    assert not [entry for entry in entries if entry.kind is SideEffectKind.POINTER_TRANSITION and entry.payload.get("action") in {"promote", "prune", "restore"} and not entry.actor]


def test_durable_actor_audit_records_ledger_actor_and_manifest_actor(tmp_path):
    app = build_durable_triage_app_for_tests(str(tmp_path / "gap7.sqlite"))
    actor = "durable-gap7-operator"
    start = app.start_run("default-user", "code-triage", "durable actor", actor=actor)
    assert start["run_manifest"].actor == actor
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "gap7-durable-candidate"})
    candidate_hash = canary["candidate"].content_hash
    app.benchmark_and_decide(candidate_hash, canary_id=canary["canary_id"], actor=actor)
    app.approve_promotion(candidate_hash, app.current_pointer_version(), actor=actor)
    app.atrophy_and_restore(candidate_hash, actor=actor)

    rows = app.db.conn.execute("SELECT kind, payload_json, actor FROM ledger ORDER BY created_at, entry_id").fetchall()
    mutation_rows = [row for row in rows if json.loads(row["payload_json"]).get("action") in {"promote", "prune", "restore"}]
    assert mutation_rows
    assert all(row["actor"] == actor for row in mutation_rows)
    assert not [row for row in mutation_rows if not row["actor"]]


def test_metrics_exposes_only_declared_counters_and_no_secrets():
    app = create_app()
    client = TestClient(app)
    session, csrf = _login(client)
    client.post("/api/action", json=_action_payload(ActionType.APPROVE_PROMOTION, pointer_version=app.state.triage.current_pointer_version(), payload={"candidate_hash": "missing"}))

    first = client.get("/api/metrics")
    second = client.get("/api/metrics")
    assert first.status_code == second.status_code == 200
    first_json = first.json()
    second_json = second.json()
    declared_counters = {
        "runs_started",
        "benchmarks_run",
        "promotions",
        "rollbacks",
        "prunes",
        "restores",
        "guardrail_breaches",
        "ui_render_failures",
        "permission_requests",
        "auth_failures",
        "privacy_violations",
    }
    assert set(first_json) == declared_counters | {"events"}
    assert set(second_json) == set(first_json)
    assert all(isinstance(first_json[name], int) for name in declared_counters)
    assert isinstance(first_json["events"], list)

    serialized = json.dumps(first_json, sort_keys=True)
    secrets = [session, csrf, "ultron-dev-run-manifest-key", "fixture-dev"]
    assert not [secret for secret in secrets if secret and secret in serialized]
    forbidden_names = ["session", "token", "csrf", "signing", "secret", "principal"]
    assert not [name for name in forbidden_names if name in serialized.lower()]


def test_vendor_integrity_fails_closed_on_drift_and_is_graceful_when_absent(tmp_path, monkeypatch):
    vendor = tmp_path / "vendor"
    (vendor / "agent").mkdir(parents=True)
    files = {
        "toolsets.py": "toolsets-ok\n",
        "agent/conversation_loop.py": "loop-ok\n",
        "agent/iteration_budget.py": "budget-ok\n",
        "agent/trajectory.py": "trajectory-ok\n",
        "agent/prompt_builder.py": "prompt-ok\n",
    }
    for relative, content in files.items():
        path = vendor / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    manifest = integrity.VendorIntegrityManifest(
        critical_files={relative: hashlib.sha256(content.encode("utf-8")).hexdigest() for relative, content in files.items()}
    )
    monkeypatch.setattr(integrity, "load_integrity_manifest", lambda path=integrity.MANIFEST_PATH: manifest)

    assert integrity.verify_vendor_integrity(vendor).status == "verified"
    (vendor / "agent" / "trajectory.py").write_text("corrupt\n", encoding="utf-8")
    with pytest.raises(VendorIntegrityError, match="sha256:agent/trajectory.py"):
        integrity.verify_vendor_integrity(vendor)

    absent = integrity.verify_vendor_integrity(tmp_path / "missing-vendor")
    assert absent.status == "vendor-absent"
    assert absent.checked_files == []
