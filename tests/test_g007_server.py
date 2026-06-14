try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover
    TestClient = None

from ultron.app.server import create_app
from ultron.evaluation.harness import GuardrailMetrics, PairedTask
from ultron.evolution.variation import VariationPrimitive


def _client():
    assert TestClient is not None
    return TestClient(create_app())


def _authed(client):
    root = client.get("/")
    return root.cookies["ultron_csrf"]


def _user_action(client, csrf, action_type, payload):
    return client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={"type": action_type, "payload": payload, "csrf_token": csrf},
    )

def _privileged(client, csrf, action_type, payload, pointer_version=None):
    version = client.app.state.triage.current_pointer_version() if pointer_version is None else pointer_version
    return client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={"type": action_type, "payload": payload, "csrf_token": csrf, "active_pointer_version": version},
    )


def test_get_root_sets_csp_tokens_and_uses_external_assets_without_inline_code():
    client = _client()
    response = client.get("/")
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "style-src 'self'" in response.headers["content-security-policy"]
    assert "ultron_session" in response.cookies
    assert "ultron_csrf" in response.cookies
    assert '<link rel="stylesheet" href="/static/chat.css">' in response.text
    assert '<script src="/static/chat.js"></script>' in response.text
    assert "<script>" not in response.text
    assert "style=" not in response.text
    assert "<style" not in response.text


def test_static_frontend_assets_are_served():
    client = _client()
    css = client.get("/static/chat.css")
    js = client.get("/static/chat.js")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]


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
    assert response.status_code == 401


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


def test_unknown_approve_promotion_denies_without_polluting_pointer_and_requests_still_work():
    client = _client()
    csrf = _authed(client)
    engine = client.app.state.triage
    before_version = engine.current_pointer_version()
    _, before_active = engine.pointer_store.get(engine.pointer_key)

    denied = _privileged(client, csrf, "APPROVE_PROMOTION", {"candidate_hash": "deadbeef"}, before_version)

    assert denied.status_code == 403
    assert "policy" in denied.json()["detail"]
    assert engine.pointer_store.get(engine.pointer_key) == (before_version, before_active)

    submitted = _user_action(client, csrf, "SUBMIT_REQUEST", {"request_text": "after deadbeef"})
    assert submitted.status_code == 200
    assert submitted.json()["envelope"]["manifest_signature_ok"] is True


def test_non_promotable_evaluated_candidate_denies_without_pointer_change():
    client = _client()
    csrf = _authed(client)
    engine = client.app.state.triage
    before_version = engine.current_pointer_version()
    _, before_active = engine.pointer_store.get(engine.pointer_key)
    canary = engine.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "negative-candidate"})
    candidate_hash = canary["candidate"].content_hash
    evaluation = engine.evaluate_and_decide(
        candidate_hash,
        [PairedTask(task_id=f"negative-{i}", baseline_metric=1.0, candidate_metric=0.9) for i in range(10)],
        canary["canary_id"],
        GuardrailMetrics(),
        GuardrailMetrics(),
    )
    assert evaluation["report"].promotable is False

    denied = _privileged(client, csrf, "APPROVE_PROMOTION", {"candidate_hash": candidate_hash}, before_version)

    assert denied.status_code == 403
    assert engine.pointer_store.get(engine.pointer_key) == (before_version, before_active)


def test_submit_request_benchmark_then_approve_promotion_advances_pointer():
    client = _client()
    csrf = _authed(client)
    engine = client.app.state.triage
    before = engine.current_pointer_version()

    submitted = _user_action(client, csrf, "SUBMIT_REQUEST", {"request_text": "triage server"})
    assert submitted.status_code == 200
    body = submitted.json()
    assert body["envelope"]["manifest_signature_ok"] is True
    candidate_hash = engine.last_candidate_hash
    assert "evaluation" not in body
    envelope = body["envelope"]
    assert envelope["manifest_signature_ok"] is True
    assert envelope["provenance"]["run"] == envelope["run_id"]
    assert envelope["components"]
    assert envelope["redaction"]["request_text"] is True
    assert envelope["redaction"]["applied"] is True
    assert envelope["provenance"]["active_pointer_version"] == str(before)
    benchmarked = _privileged(client, csrf, "RUN_BENCHMARK", {"candidate_hash": candidate_hash, "canary_id": body["canary_id"]})
    assert benchmarked.status_code == 200
    assert engine.has_promotable_evidence(candidate_hash)
    assert engine.current_pointer_version() == before

    approved = _privileged(client, csrf, "APPROVE_PROMOTION", {"candidate_hash": candidate_hash})

    assert approved.status_code == 200
    assert approved.json()["promoted"] is True
    assert engine.current_pointer_version() == before + 1


def test_submit_request_and_stale_privileged_rejected():
    client = _client()
    csrf = _authed(client)
    submitted = _user_action(client, csrf, "SUBMIT_REQUEST", {"request_text": "triage server"})
    assert submitted.status_code == 200
    candidate_hash = client.app.state.triage.last_candidate_hash
    stale = _privileged(client, csrf, "APPROVE_PROMOTION", {"candidate_hash": candidate_hash}, 0)
    assert stale.status_code == 403



def test_submit_request_and_give_feedback_require_session_csrf_and_record_actor():
    client = _client()
    missing_session = client.post("/api/action", json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "auth gate"}})
    assert missing_session.status_code == 401

    csrf = _authed(client)
    missing_csrf = client.post("/api/action", json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "auth gate"}})
    assert missing_csrf.status_code == 403

    submitted = _user_action(client, csrf, "SUBMIT_REQUEST", {"request_text": "auth gate"})
    assert submitted.status_code == 200
    assert submitted.json()["envelope"]["run_id"]
    run_id = submitted.json()["envelope"]["run_id"]
    run_entries = client.app.state.triage.ledger.entries_for_run(run_id)
    assert run_entries and {entry.actor for entry in run_entries} == {"local-operator"}

    no_feedback_session = _client().post("/api/action", json={"type": "GIVE_FEEDBACK", "payload": {"run_id": run_id, "rating": 1}})
    assert no_feedback_session.status_code == 401
    no_feedback_csrf = client.post("/api/action", json={"type": "GIVE_FEEDBACK", "payload": {"run_id": run_id, "rating": 1}})
    assert no_feedback_csrf.status_code == 403

    feedback = _user_action(client, csrf, "GIVE_FEEDBACK", {"run_id": run_id, "rating": 1, "comment": "keep"})
    assert feedback.status_code == 200
    feedback_entries = [entry for entry in client.app.state.triage.ledger.entries_for_run(run_id) if entry.kind.value == "FEEDBACK_EVENT"]
    assert feedback_entries and feedback_entries[-1].actor == "local-operator"
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
    assert active.json()["status"] == "rollback_complete"

    inactive = _privileged(client, csrf, "ROLLBACK_CANARY", {"canary_id": canary["canary_id"]})
    assert inactive.status_code == 403


def test_permission_expansion_is_recorded_pending_not_applied():
    client = _client()
    csrf = _authed(client)
    engine = client.app.state.triage

    response = _privileged(client, csrf, "REQUEST_PERMISSION_EXPANSION", {"tool": "network", "reason": "debug"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending_human_approval"
    assert engine.pending_permission_expansions[-1]["request_id"].startswith(body["request_id"])


def test_raw_unknown_ui_panel_injection_attempt_rejected():
    client = _client()
    response = client.post(
        "/api/action",
        json={"type": "SUBMIT_REQUEST", "payload": {}, "components": [{"type": "EVIL_PANEL"}]},
    )
    assert response.status_code == 422
