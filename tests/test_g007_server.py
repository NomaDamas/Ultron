try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover
    TestClient = None

from ultron.app.server import create_app
from ultron.evolution.variation import VariationPrimitive


def _client():
    assert TestClient is not None
    return TestClient(create_app())


def _authed(client):
    root = client.get("/")
    return root.cookies["ultron_csrf"]


def _privileged(client, csrf, action_type, payload, pointer_version=None):
    version = client.app.state.triage.current_pointer_version() if pointer_version is None else pointer_version
    return client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={"type": action_type, "payload": payload, "csrf_token": csrf, "active_pointer_version": version},
    )


def test_get_root_sets_csp_tokens_and_uses_external_script():
    client = _client()
    response = client.get("/")
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "ultron_session" in response.cookies
    assert "ultron_csrf" in response.cookies
    assert '<script src="/static/app.js"></script>' in response.text
    assert "<script>" not in response.text


def test_get_uispec_returns_valid_spec():
    client = _client()
    response = client.get("/api/uispec")
    assert response.status_code == 200
    body = response.json()
    assert body["spec_hash"]
    assert body["components"]


def test_unknown_action_type_422_and_privileged_without_csrf_403():
    client = _client()
    assert client.post("/api/action", json={"type": "NOT_REAL", "payload": {}}).status_code == 422
    response = client.post("/api/action", json={"type": "APPROVE_PROMOTION", "payload": {}, "active_pointer_version": 1})
    assert response.status_code == 403


def test_approve_promotion_without_promotable_evidence_denies_and_does_not_advance_pointer():
    client = _client()
    csrf = _authed(client)
    engine = client.app.state.triage
    before = engine.current_pointer_version()
    canary = engine.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "unevaluated-candidate"})
    candidate_hash = canary["candidate"].content_hash

    denied = _privileged(client, csrf, "APPROVE_PROMOTION", {"candidate_hash": candidate_hash}, before)

    assert denied.status_code == 403
    assert "policy" in denied.json()["detail"]
    assert engine.current_pointer_version() == before


def test_submit_request_creates_evaluates_and_approve_promotion_advances_pointer():
    client = _client()
    csrf = _authed(client)
    engine = client.app.state.triage
    before = engine.current_pointer_version()

    submitted = client.post("/api/action", json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "triage server"}})
    assert submitted.status_code == 200
    body = submitted.json()
    assert body["result"]["run_manifest"]["signature"]
    candidate_hash = body["candidate"]["content_hash"]
    assert body["evaluation"]["report"]["promotable"] is True
    assert body["evaluation"]["report"]["evidence_label"] == "benchmark_evidence"
    assert engine.current_pointer_version() == before

    approved = _privileged(client, csrf, "APPROVE_PROMOTION", {"candidate_hash": candidate_hash})

    assert approved.status_code == 200
    assert approved.json()["decision"]["promoted"] is True
    assert engine.current_pointer_version() == before + 1


def test_submit_request_and_stale_privileged_rejected():
    client = _client()
    csrf = _authed(client)
    submitted = client.post("/api/action", json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "triage server"}})
    assert submitted.status_code == 200
    candidate_hash = submitted.json()["candidate"]["content_hash"]
    stale = _privileged(client, csrf, "APPROVE_PROMOTION", {"candidate_hash": candidate_hash}, 0)
    assert stale.status_code == 403


def test_rollback_canary_policy_denies_missing_and_allows_active_canary():
    client = _client()
    csrf = _authed(client)
    engine = client.app.state.triage

    missing = _privileged(client, csrf, "ROLLBACK_CANARY", {"canary_id": "canary-missing"})
    assert missing.status_code == 403
    assert "policy" in missing.json()["detail"]

    canary = engine.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "rollback-candidate"})
    active = _privileged(client, csrf, "ROLLBACK_CANARY", {"canary_id": canary["canary_id"]})
    assert active.status_code == 200
    assert active.json()["rollback"]["dropped_namespaces"]

    inactive = _privileged(client, csrf, "ROLLBACK_CANARY", {"canary_id": canary["canary_id"]})
    assert inactive.status_code == 403


def test_permission_expansion_is_recorded_pending_not_applied():
    client = _client()
    csrf = _authed(client)
    engine = client.app.state.triage

    response = _privileged(client, csrf, "REQUEST_PERMISSION_EXPANSION", {"tool": "network", "reason": "debug"})

    assert response.status_code == 200
    request = response.json()["permission_expansion"]
    assert request["status"] == "pending_human_approval"
    assert engine.pending_permission_expansions == [request]


def test_raw_unknown_ui_panel_injection_attempt_rejected():
    client = _client()
    response = client.post(
        "/api/action",
        json={"type": "SUBMIT_REQUEST", "payload": {}, "components": [{"type": "EVIL_PANEL"}]},
    )
    assert response.status_code == 422
