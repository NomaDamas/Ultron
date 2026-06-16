"""Story E: regression hardening — secrets-never-leak canary matrix, fail-closed
matrix, model-output validation, and default-fake green smoke."""

from __future__ import annotations

import base64
import io
import json
import sys
import types
from pathlib import Path

import pytest

from ultron.config import ConfigService, ModelSettingsWrite
from ultron.config.secrets import SecretStore
from ultron.images import MAX_BYTES, ImageRejected, validate_image
from ultron.model_provider import (
    DeterministicFakeLlmProvider,
    ImagePart,
    LiveModelProviderError,
    ModelMessage,
    ModelProviderConfig,
    ModelRole,
    OpenAICompatibleLlmProvider,
    OpenAICompatibleVlmProvider,
    RawImagePayload,
    TextPart,
)
from ultron.ui.generator import LiveModelUiSpecGenerator, LiveModelUnavailable, UiGenContext
from ultron.ui.runtime import ComponentType

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "ultron" / "app" / "static"
CANARY = "sk-CANARY-ultron-SECRET-7777"

try:
    from fastapi.testclient import TestClient

    from ultron.app.server import create_app
except Exception:  # pragma: no cover
    TestClient = None  # type: ignore

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


def _png_bytes(width=8, height=8):
    img = Image.new("RGB", (width, height), (12, 34, 56))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Secrets-never-leak canary matrix
# ---------------------------------------------------------------------------


def test_canary_secret_store_read_surfaces(tmp_path):
    svc = ConfigService(store=SecretStore(tmp_path / "s.json"), environ={}, dotenv={})
    svc.apply_write(ModelSettingsWrite(llm_api_key=CANARY, llm_model="m", llm_base_url="https://h.example/v1"), actor="op")
    read_blob = json.dumps(svc.model_settings_read().model_dump(mode="json"))
    audit_blob = json.dumps(svc.audit)
    assert CANARY not in read_blob
    assert CANARY not in audit_blob


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_canary_server_settings_and_telemetry(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    client = TestClient(create_app())
    csrf = client.get("/dashboard").cookies["ultron_csrf"]
    post = client.post("/api/settings/model", headers={"X-CSRF-Token": csrf}, json={"llm_api_key": CANARY, "llm_model": "m"})
    assert post.status_code == 200
    for blob in [post.text, client.get("/api/settings/model").text, json.dumps(client.app.state.triage.telemetry.snapshot()), client.get("/api/metrics").text, client.get("/api/ledger").text]:
        assert CANARY not in blob


def test_canary_cli_stdout_stderr(tmp_path, capsys):
    from ultron.config.__main__ import main as config_cli

    svc = ConfigService(store=SecretStore(tmp_path / "s.json"), environ={}, dotenv={})
    monkey_stdin = io.StringIO(CANARY + "\n")
    old = sys.stdin
    sys.stdin = monkey_stdin
    try:
        config_cli(["set", "llm.api_key", "--stdin"], service=svc, out=io.StringIO())
    finally:
        sys.stdin = old
    config_cli(["status"], service=svc, out=io.StringIO())
    config_cli(["get", "llm.api_key"], service=svc, out=io.StringIO())
    captured = capsys.readouterr()
    assert CANARY not in captured.out
    assert CANARY not in captured.err


def test_canary_provider_exception(monkeypatch):
    class _Exploder(types.ModuleType):
        def __init__(self):
            super().__init__("httpx")

        def post(self, *a, **k):
            raise RuntimeError(f"body with {CANARY} at https://secret.invalid")

    monkeypatch.setitem(sys.modules, "httpx", _Exploder())
    provider = OpenAICompatibleLlmProvider(ModelProviderConfig(base_url="https://secret.invalid/v1", api_key=CANARY, model_name="m"))
    with pytest.raises(LiveModelProviderError) as exc:
        provider.complete_text([ModelMessage(ModelRole.USER, [TextPart("hi")])])
    assert CANARY not in str(exc.value)
    assert exc.value.__context__ is None


@pytest.mark.skipif(Image is None, reason="Pillow unavailable")
def test_canary_image_paths_no_raw_leak():
    # accepted path: durable metadata never contains the raw data url
    part = validate_image(_png_bytes())
    assert "data:image" not in json.dumps(part.metadata())
    # rejected path: generic message only
    with pytest.raises(ImageRejected) as e:
        validate_image(b"GIF89a" + b"x" * 50)
    assert str(e.value) == "unsupported image type"


def test_dashboard_js_never_hydrates_raw_key():
    js = (STATIC / "dashboard.js").read_text()
    # the settings panel must use SecretRef labels and password inputs, never echo raw keys
    assert "keyRefLabel" in js
    assert "inputs.llm_api_key.value = ''" in js
    for forbidden in ["innerHTML", "eval(", "new Function", ".style", "document.write"]:
        assert forbidden not in js


# ---------------------------------------------------------------------------
# Fail-closed matrix
# ---------------------------------------------------------------------------


def test_fail_closed_partial_config():
    provider = OpenAICompatibleLlmProvider(ModelProviderConfig(base_url="u", api_key="k"))  # no model
    with pytest.raises(LiveModelUnavailable):
        provider.complete_text([ModelMessage(ModelRole.USER, [TextPart("x")])])


def test_fail_closed_image_to_text_only_llm():
    image = ImagePart(mime_type="image/png", byte_length=4, width=8, height=8, pixel_count=64, fingerprint="f", _raw=RawImagePayload(b"x", "data:image/png;base64,AA"))
    provider = OpenAICompatibleLlmProvider(ModelProviderConfig(base_url="u", api_key="k", model_name="m"))
    with pytest.raises(LiveModelUnavailable):
        provider.complete_text([ModelMessage(ModelRole.USER, [TextPart("x"), image])])


def test_fail_closed_vlm_missing_config():
    provider = OpenAICompatibleVlmProvider(ModelProviderConfig())
    with pytest.raises(LiveModelUnavailable):
        provider.complete_multimodal([ModelMessage(ModelRole.USER, [TextPart("x")])])


def test_fail_closed_model_generator_invalid_json():
    gen = LiveModelUiSpecGenerator(DeterministicFakeLlmProvider(responder=lambda m, h: "{not valid"))

    class _M:
        resolved_ui_panels = []

    with pytest.raises(ValueError):
        gen.generate(UiGenContext(module_set_manifest=_M(), request_class="triage", allowed_registry=list(ComponentType)))


def test_fail_closed_model_generator_no_provider():
    gen = LiveModelUiSpecGenerator(None)

    class _M:
        resolved_ui_panels = []

    with pytest.raises(LiveModelUnavailable):
        gen.generate(UiGenContext(module_set_manifest=_M(), request_class="triage"))


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_fail_closed_live_ui_selected_never_fake(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    for key in ["ULTRON_LLM_BASE_URL", "ULTRON_LLM_API_KEY", "ULTRON_LLM_MODEL", "ULTRON_MODEL_BASE_URL", "ULTRON_MODEL_API_KEY", "ULTRON_MODEL_NAME"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ULTRON_ADAPTER", "fake")
    monkeypatch.setenv("ULTRON_UI_GENERATOR", "model")
    client = TestClient(create_app())
    csrf = client.get("/").cookies["ultron_csrf"]
    resp = client.post("/api/action", headers={"X-CSRF-Token": csrf}, json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "x"}, "csrf_token": csrf})
    assert resp.status_code == 503
    body = resp.json()
    assert "components" not in body
    # never silently fell back to a fake-generated envelope
    assert "envelope" not in body


# ---------------------------------------------------------------------------
# Default fake mode stays green
# ---------------------------------------------------------------------------


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_default_fake_mode_produces_validated_envelope(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    for key in ["ULTRON_ADAPTER", "ULTRON_UI_GENERATOR", "ULTRON_MODULE_SYNTH", "ULTRON_VLM"]:
        monkeypatch.delenv(key, raising=False)
    client = TestClient(create_app())
    csrf = client.get("/").cookies["ultron_csrf"]
    resp = client.post("/api/action", headers={"X-CSRF-Token": csrf}, json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "build a tool"}, "csrf_token": csrf})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["envelope"]["components"]
