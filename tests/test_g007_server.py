try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover
    TestClient = None

from ultron.app.server import create_app


def _client():
    assert TestClient is not None
    return TestClient(create_app())


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


def test_submit_request_and_stale_privileged_rejected():
    client = _client()
    root = client.get("/")
    csrf = root.cookies["ultron_csrf"]
    submitted = client.post("/api/action", json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "triage server"}})
    assert submitted.status_code == 200
    assert submitted.json()["result"]["run_manifest"]["signature"]
    stale = client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={"type": "APPROVE_PROMOTION", "payload": {}, "csrf_token": csrf, "active_pointer_version": 0},
    )
    assert stale.status_code == 403
