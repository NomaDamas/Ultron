from pathlib import Path

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover
    TestClient = None

from ultron.app.server import create_app

ROOT = Path(__file__).resolve().parents[1]
CHAT_JS = ROOT / "src" / "ultron" / "app" / "static" / "chat.js"
CHAT_CSS = ROOT / "src" / "ultron" / "app" / "static" / "chat.css"
DASHBOARD_JS = ROOT / "src" / "ultron" / "app" / "static" / "dashboard.js"


def _client():
    assert TestClient is not None
    return TestClient(create_app())


def test_root_shell_is_strict_external_chat_only_page():
    response = _client().get("/")

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert '<link rel="stylesheet" href="/static/chat.css">' in response.text
    assert '<script src="/static/chat.js"></script>' in response.text
    assert "<script>" not in response.text
    assert "style=" not in response.text
    assert "<style" not in response.text
    assert "data-csrf-cookie" in response.text
    forbidden_shell = ["Your tools", "toolbelt", "ecology", "runs", "ledger", "metrics", "sidebar"]
    for marker in forbidden_shell:
        assert marker not in response.text


def test_chat_js_does_not_render_primary_ops_or_toolbelt_on_root():
    source = CHAT_JS.read_text()

    forbidden = ["Your tools", "toolbelt", "tool-list", "/api/toolbelt", "/api/ecology", "/api/runs", "/api/ledger"]
    for marker in forbidden:
        assert marker not in source
    assert "/api/action" in source
    assert "createElement" in source
    assert "textContent" in source


def test_dashboard_still_owns_ops_surfaces():
    source = DASHBOARD_JS.read_text()

    for marker in ["/api/ecology", "/api/runs", "/api/ledger", "/api/metrics", "Gated controls"]:
        assert marker in source


def test_chat_css_contains_jarvis_tokens_animations_and_reduced_motion():
    client = _client()
    response = client.get("/static/chat.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    source = response.text

    for marker in [
        "--bg: #0a0e1a",
        "--accent: #00d4ff",
        "--accent-2: #00b4d8",
        "backdrop-filter",
        "::before",
        "::after",
        ".ultron-orb",
        ".anim-fade-in",
        ".anim-slide-up",
        ".anim-pulse-glow",
        ".anim-reticle-scan",
        ".anim-expand",
        "@media (prefers-reduced-motion: reduce)",
    ]:
        assert marker in source


def test_chat_js_animation_allowlist_and_csp_safe_static_scan():
    source = CHAT_JS.read_text()

    for marker in [
        "const ANIMATION_CLASS",
        "none: ''",
        "fade_in: 'anim-fade-in'",
        "slide_up: 'anim-slide-up'",
        "pulse_glow: 'anim-pulse-glow'",
        "reticle_scan: 'anim-reticle-scan'",
        "expand: 'anim-expand'",
        "classList.add(className)",
        "prefers-reduced-motion: reduce",
    ]:
        assert marker in source
    for forbidden in ["innerHTML", "eval(", "new Function", ".style", "setAttribute('style'", 'setAttribute("style"']:
        assert forbidden not in source
