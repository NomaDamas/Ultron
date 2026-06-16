"""Story D: command bar + bounded A/B generative-UI canvas with image picker."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHAT_JS = ROOT / "src" / "ultron" / "app" / "static" / "chat.js"
CHAT_CSS = ROOT / "src" / "ultron" / "app" / "static" / "chat.css"

try:
    from fastapi.testclient import TestClient

    from ultron.app.server import create_app
except Exception:  # pragma: no cover
    TestClient = None  # type: ignore


def _js() -> str:
    return CHAT_JS.read_text()


def test_command_bar_and_canvas_present():
    src = _js()
    for marker in ["command-bar", "command-input", "command-controls", "canvas", "canvas-empty", "canvas-group", "createShell()"]:
        assert marker in src, marker
    # the old scrolling-thread transcript model is gone
    assert "appendUserTurn" not in src
    assert "user-bubble" not in src


def test_ab_mode_toggle_and_bounded_reducer():
    src = _js()
    assert "mode-toggle" in src
    assert "state.mode === 'replace'" in src
    assert "MAX_CANVAS_ENVELOPES = 20" in src
    assert "MAX_CANVAS_CARDS = 120" in src
    assert "function reduceCanvas" in src
    assert "function enforceCanvasCaps" in src
    # REPLACE clears prior cards; ACCUMULATE appends and enforces caps
    assert "canvas.textContent = ''" in src
    assert "enforceCanvasCaps(canvas)" in src


def test_image_picker_and_no_raw_retention():
    src = _js()
    assert "image-input" in src
    assert "readAsDataURL" in src
    assert "image_base64" in src
    assert "MAX_IMAGE_BYTES" in src
    # raw image reference dropped immediately after sending; not retained in state
    assert "clearPendingImage()" in src
    assert "state.pendingImage = null" in src


def test_chat_js_remains_csp_safe():
    src = _js()
    for forbidden in ["innerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write", ".style", "setAttribute('style'", 'setAttribute("style"']:
        assert forbidden not in src, forbidden
    assert ".textContent" in src
    assert "createElement" in src


def test_animation_allowlist_preserved_in_css():
    css = CHAT_CSS.read_text()
    assert set(re.findall(r"\.anim-([a-z-]+)\b", css)) == {"fade-in", "slide-up", "pulse-glow", "reticle-scan", "expand"}
    assert "prefers-reduced-motion" in css


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_root_shell_is_strict_and_loads_canvas_assets():
    client = TestClient(create_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert '<link rel="stylesheet" href="/static/chat.css">' in resp.text
    assert '<script src="/static/chat.js"></script>' in resp.text
    # No inline script/style anywhere in the served shell.
    assert "<script>" not in resp.text
    assert "<style" not in resp.text
    assert "style=" not in resp.text
    assert "default-src 'self'" in resp.headers["content-security-policy"]
