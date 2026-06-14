import json

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover
    TestClient = None

from ultron.app.server import create_app
from ultron.app.triage import TriageApp



def _client():
    assert TestClient is not None
    return TestClient(create_app())


def _assert_strict_shell(response, css, js):
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "style-src 'self'" in response.headers["content-security-policy"]
    assert f'<link rel="stylesheet" href="{css}">' in response.text
    assert f'<script src="{js}"></script>' in response.text
    assert "<script>" not in response.text
    assert "style=" not in response.text
    assert "<style" not in response.text
    assert "data-csrf-cookie" in response.text


def test_chat_and_dashboard_shells_are_strict_external_asset_pages():
    client = _client()
    _assert_strict_shell(client.get("/"), "/static/chat.css", "/static/chat.js")
    _assert_strict_shell(client.get("/dashboard"), "/static/dashboard.css", "/static/dashboard.js")


STATIC_ASSETS = [
    "/static/chat.css",
    "/static/chat.js",
    "/static/dashboard.css",
    "/static/dashboard.js",
]


def test_uiv2_static_assets_are_served():
    client = _client()
    for path in STATIC_ASSETS:
        response = client.get(path)
        assert response.status_code == 200
        if path.endswith(".css"):
            assert "text/css" in response.headers["content-type"]
        else:
            assert "javascript" in response.headers["content-type"]


def test_read_only_endpoints_return_documented_no_secret_json():
    client = _client()
    csrf = client.get("/").cookies["ultron_csrf"]
    submitted = client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "shape my review harness"}, "csrf_token": csrf},
    )
    assert submitted.status_code == 200

    endpoints = ["/api/toolbelt", "/api/ecology", "/api/runs", "/api/ledger"]
    bodies = {}
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 200
        assert "default-src 'self'" in response.headers["content-security-policy"]
        bodies[endpoint] = response.json()

    tool = bodies["/api/toolbelt"]["modules"][0]
    assert {"name", "module_id", "version", "target_lens", "workflow_tags", "fitness"}.issubset(tool)
    assert {"usage_count", "promotion_state", "primary_metric"}.issubset(tool["fitness"])

    ecology = bodies["/api/ecology"]
    assert {"seed", "candidate", "survivor", "decaying", "pruned", "quarantined"}.issubset(ecology["modules_by_lifecycle"])
    candidate = ecology["modules_by_lifecycle"]["candidate"][0]
    assert {"module_id", "version", "content_hash", "parent_id", "fitness"}.issubset(candidate)
    assert len(candidate["content_hash"]) <= 12
    assert candidate["parent_id"] is not None
    assert len(candidate["parent_id"]) == len(candidate["content_hash"]) == 12
    assert candidate["parent_id"] != candidate["content_hash"]
    assert all(item["parent_id"] is not None and len(item["parent_id"]) == 12 and len(item["child_id"]) == 12 for item in ecology["lineage"])
    assert any(item["parent_id"] == candidate["parent_id"] and item["child_id"] == candidate["content_hash"] for item in ecology["lineage"])
    assert all(len(value) != 64 for item in ecology["lineage"] for value in (item["parent_id"], item["child_id"]) if value)
    assert "active_pointer_version" in ecology
    assert "lineage" in ecology

    run = bodies["/api/runs"]["runs"][0]
    assert {"run_id", "workflow", "active_module_set_hash", "model_snapshot", "created_at", "trajectory_id"}.issubset(run)
    assert set(run["model_snapshot"]) == {"provider", "name"}

    ledger = bodies["/api/ledger"]["entries"][0]
    assert {"entry_id", "kind", "module_hash", "canary_id", "actor", "created_at", "quarantined"}.issubset(ledger)
    assert "payload" not in ledger

    serialized = json.dumps(bodies).lower()
    forbidden = ["signing", "api_key", "csrf", "session", csrf.lower(), "ultron-dev-run-manifest-key"]
    for marker in forbidden:
        assert marker not in serialized


def test_read_snapshots_on_unseeded_app_are_empty_and_do_not_seed():
    app = TriageApp()

    assert app.pointer_store.get(app.pointer_key) == (0, [])
    assert app.active_modules() == []
    ecology = app.modules_by_lifecycle()
    assert {"seed", "candidate", "survivor", "decaying", "pruned", "quarantined"}.issubset(ecology)
    assert all(modules == [] for modules in ecology.values())
    assert app.lineage_view() == []
    assert app.recent_runs() == []
    assert app.recent_ledger() == []
    assert app.safety_status()["active_pointer_version"] == 0
    assert app.pointer_store.get(app.pointer_key) == (0, [])
    assert app.registry._entries == {}
