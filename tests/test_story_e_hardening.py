"""Story E: regression hardening — secrets-never-leak canary matrix, fail-closed
matrix, model-output validation, and default-fake green smoke.

The canary is high-entropy and generated per run; a single helper drives it across
every plan-listed read/observability surface and asserts absence everywhere.
"""

from __future__ import annotations

import io
import json
import secrets as secretslib
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
from ultron.synthesis.module_synthesizer import LiveModelModuleSynthesizer
from ultron.ui.generator import LiveModelUiSpecGenerator, LiveModelUnavailable, UiGenContext
from ultron.ui.runtime import ComponentType, InlineGenUiEnvelope, validate_inline_envelope

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "ultron" / "app" / "static"

LLM_ENV = [
    "ULTRON_LLM_BASE_URL", "ULTRON_LLM_API_KEY", "ULTRON_LLM_MODEL",
    "ULTRON_VLM_BASE_URL", "ULTRON_VLM_API_KEY", "ULTRON_VLM_MODEL",
    "ULTRON_MODEL_BASE_URL", "ULTRON_MODEL_API_KEY", "ULTRON_MODEL_NAME",
]

try:
    from fastapi.testclient import TestClient

    from ultron.app.server import create_app
except Exception:  # pragma: no cover
    TestClient = None  # type: ignore

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


def _canary() -> str:
    return "sk-" + secretslib.token_hex(24)


def _png_bytes(width=8, height=8):
    img = Image.new("RGB", (width, height), (12, 34, 56))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Secrets-never-leak canary matrix
# ---------------------------------------------------------------------------


def test_canary_secret_store_read_and_audit(tmp_path):
    canary = _canary()
    svc = ConfigService(store=SecretStore(tmp_path / "s.json"), environ={}, dotenv={})
    svc.apply_write(ModelSettingsWrite(llm_api_key=canary, llm_model="m", llm_base_url="https://h.example/v1"), actor="op")
    assert canary not in json.dumps(svc.model_settings_read().model_dump(mode="json"))
    assert canary not in json.dumps(svc.audit)


@pytest.mark.skipif(TestClient is None or Image is None, reason="fastapi/Pillow unavailable")
def test_canary_absent_across_all_server_surfaces(monkeypatch, tmp_path):
    canary = _canary()
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    client = TestClient(create_app())
    csrf = client.get("/dashboard").cookies["ultron_csrf"]

    # Introduce the canary through every write path: settings key, request text,
    # feedback comment, and an image-bearing command.
    assert client.post("/api/settings/model", headers={"X-CSRF-Token": csrf}, json={"llm_api_key": canary, "llm_model": "m"}).status_code == 200
    image_b64 = __import__("base64").b64encode(_png_bytes()).decode("ascii")
    submit = client.post("/api/action", headers={"X-CSRF-Token": csrf}, json={"type": "SUBMIT_REQUEST", "payload": {"request_text": f"analyze token {canary}", "image_base64": image_b64}, "csrf_token": csrf})
    assert submit.status_code == 200
    run_id = submit.json()["envelope"]["run_id"]
    client.post("/api/action", headers={"X-CSRF-Token": csrf}, json={"type": "GIVE_FEEDBACK", "payload": {"run_id": run_id, "rating": -1, "comment": f"bad because {canary}"}, "csrf_token": csrf})
    # A validation-error path with the canary embedded must also redact it.
    client.post("/api/action", headers={"X-CSRF-Token": csrf}, json={"type": "NOT_A_REAL_ACTION", "payload": {"x": canary}, "csrf_token": csrf})

    surfaces = [
        client.get("/").text,
        client.get("/dashboard").text,
        client.get("/static/chat.js").text,
        client.get("/static/dashboard.js").text,
        client.get("/api/settings/model").text,
        client.get("/api/runs").text,
        client.get("/api/ecology").text,
        client.get("/api/personalization").text,
        client.get("/api/toolbelt").text,
        client.get("/api/uispec").text,
        client.get("/api/ledger").text,
        client.get("/api/metrics").text,
        json.dumps(client.app.state.triage.telemetry.snapshot()),
        submit.text,
    ]
    for blob in surfaces:
        assert canary not in blob


def test_canary_cli_streams_and_process_output(tmp_path, capsys):
    from ultron.config.__main__ import main as config_cli

    canary = _canary()
    svc = ConfigService(store=SecretStore(tmp_path / "s.json"), environ={}, dotenv={})
    old = sys.stdin
    sys.stdin = io.StringIO(canary + "\n")
    try:
        set_out = io.StringIO()
        config_cli(["set", "llm.api_key", "--stdin"], service=svc, out=set_out)
    finally:
        sys.stdin = old
    status_out = io.StringIO()
    config_cli(["status"], service=svc, out=status_out)
    get_out = io.StringIO()
    config_cli(["get", "llm.api_key"], service=svc, out=get_out)
    captured = capsys.readouterr()
    for stream in [set_out.getvalue(), status_out.getvalue(), get_out.getvalue(), captured.out, captured.err]:
        assert canary not in stream


def test_canary_provider_exception_sanitized(monkeypatch):
    canary = _canary()

    class _Exploder(types.ModuleType):
        def __init__(self):
            super().__init__("httpx")

        def post(self, *a, **k):
            raise RuntimeError(f"body with {canary} at https://secret.invalid")

    monkeypatch.setitem(sys.modules, "httpx", _Exploder())
    provider = OpenAICompatibleLlmProvider(ModelProviderConfig(base_url="https://secret.invalid/v1", api_key=canary, model_name="m"))
    with pytest.raises(LiveModelProviderError) as exc:
        provider.complete_text([ModelMessage(ModelRole.USER, [TextPart("hi")])])
    assert canary not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None


@pytest.mark.skipif(Image is None, reason="Pillow unavailable")
def test_canary_image_paths_no_raw_leak():
    part = validate_image(_png_bytes())
    assert "data:image" not in json.dumps(part.metadata())
    with pytest.raises(ImageRejected) as e:
        validate_image(b"GIF89a" + b"x" * 50)
    assert str(e.value) == "unsupported image type"


def test_dashboard_js_never_hydrates_raw_key():
    js = (STATIC / "dashboard.js").read_text()
    assert "keyRefLabel" in js
    assert "inputs.llm_api_key.value = ''" in js
    for forbidden in ["innerHTML", "eval(", "new Function", ".style", "document.write"]:
        assert forbidden not in js


# ---------------------------------------------------------------------------
# Fail-closed matrix
# ---------------------------------------------------------------------------


def test_fail_closed_partial_llm_config():
    provider = OpenAICompatibleLlmProvider(ModelProviderConfig(base_url="u", api_key="k"))  # no model
    with pytest.raises(LiveModelUnavailable):
        provider.complete_text([ModelMessage(ModelRole.USER, [TextPart("x")])])


def test_fail_closed_partial_vlm_config():
    provider = OpenAICompatibleVlmProvider(ModelProviderConfig(base_url="u", api_key="k"))  # no model
    with pytest.raises(LiveModelUnavailable):
        provider.complete_multimodal([ModelMessage(ModelRole.USER, [TextPart("x")])])


def test_fail_closed_image_to_text_only_llm():
    image = ImagePart(mime_type="image/png", byte_length=4, width=8, height=8, pixel_count=64, fingerprint="f", _raw=RawImagePayload(b"x", "data:image/png;base64,AA"))
    provider = OpenAICompatibleLlmProvider(ModelProviderConfig(base_url="u", api_key="k", model_name="m"))
    with pytest.raises(LiveModelUnavailable):
        provider.complete_text([ModelMessage(ModelRole.USER, [TextPart("x"), image])])


class _FakeManifest:
    resolved_ui_panels = []


def test_fail_closed_model_uispec_invalid_json():
    gen = LiveModelUiSpecGenerator(DeterministicFakeLlmProvider(responder=lambda m, h: "{not valid"))
    with pytest.raises(ValueError):
        gen.generate(UiGenContext(module_set_manifest=_FakeManifest(), request_class="triage", allowed_registry=list(ComponentType)))


def test_fail_closed_model_uispec_invalid_component_rejected():
    bogus = json.dumps({"components": [{"type": "TOTALLY_FAKE_PANEL", "region": "main", "priority": 1, "props": {}}]})
    gen = LiveModelUiSpecGenerator(DeterministicFakeLlmProvider(responder=lambda m, h: bogus))
    with pytest.raises(ValueError):
        gen.generate(UiGenContext(module_set_manifest=_FakeManifest(), request_class="triage", allowed_registry=list(ComponentType)))


def test_fail_closed_model_module_synth_invalid_json():
    synth = LiveModelModuleSynthesizer(DeterministicFakeLlmProvider(responder=lambda m, h: "{nope"))
    # no provider also fails closed
    assert LiveModelModuleSynthesizer(None).provider is None
    from ultron.synthesis.module_synthesizer import SynthesisContext, SynthesisPolicyConstraints
    from ultron.hermes.module_surface_contract import ModuleSurfaceContract

    ctx = SynthesisContext(
        request_text="x",
        workflow_fingerprint="wf",
        policy_constraints=SynthesisPolicyConstraints(allowed_surfaces=ModuleSurfaceContract()),
    )
    with pytest.raises(ValueError):
        synth.synthesize(ctx)


def test_fail_closed_model_synth_no_provider():
    from ultron.synthesis.module_synthesizer import SynthesisContext, SynthesisPolicyConstraints
    from ultron.hermes.module_surface_contract import ModuleSurfaceContract

    ctx = SynthesisContext(request_text="x", workflow_fingerprint="wf", policy_constraints=SynthesisPolicyConstraints(allowed_surfaces=ModuleSurfaceContract()))
    with pytest.raises(LiveModelUnavailable):
        LiveModelModuleSynthesizer(None).synthesize(ctx)


@pytest.mark.skipif(TestClient is None or Image is None, reason="fastapi/Pillow unavailable")
def test_fail_closed_oversized_and_unsupported_image_api(monkeypatch, tmp_path):
    import base64

    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    client = TestClient(create_app())
    csrf = client.get("/").cookies["ultron_csrf"]
    unsupported = base64.b64encode(b"GIF89a" + b"x" * 64).decode("ascii")
    r1 = client.post("/api/action", headers={"X-CSRF-Token": csrf}, json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "x", "image_base64": unsupported}, "csrf_token": csrf})
    assert r1.status_code == 422 and "unsupported image type" in r1.text
    oversized = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_BYTES + 1)).decode("ascii")
    r2 = client.post("/api/action", headers={"X-CSRF-Token": csrf}, json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "x", "image_base64": oversized}, "csrf_token": csrf})
    assert r2.status_code == 422 and "too large" in r2.text


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_fail_closed_live_ui_selected_never_fake(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    for key in LLM_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ULTRON_ADAPTER", "fake")
    monkeypatch.setenv("ULTRON_UI_GENERATOR", "model")
    client = TestClient(create_app())
    csrf = client.get("/").cookies["ultron_csrf"]
    resp = client.post("/api/action", headers={"X-CSRF-Token": csrf}, json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "x"}, "csrf_token": csrf})
    assert resp.status_code == 503
    body = resp.json()
    assert "components" not in body and "envelope" not in body


# ---------------------------------------------------------------------------
# Default fake mode stays green AND server-validated
# ---------------------------------------------------------------------------


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_default_fake_mode_produces_server_validated_envelope(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    for key in [*LLM_ENV, "ULTRON_ADAPTER", "ULTRON_UI_GENERATOR", "ULTRON_MODULE_SYNTH", "ULTRON_VLM"]:
        monkeypatch.delenv(key, raising=False)
    client = TestClient(create_app())
    csrf = client.get("/").cookies["ultron_csrf"]
    resp = client.post("/api/action", headers={"X-CSRF-Token": csrf}, json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "build a tool"}, "csrf_token": csrf})
    assert resp.status_code == 200
    envelope_dict = resp.json()["envelope"]
    # Reconstruct and re-run the server validation contract on the returned envelope.
    envelope = InlineGenUiEnvelope.model_validate(envelope_dict)
    validate_inline_envelope(envelope, list(ComponentType))
    assert envelope_dict.get("envelope_hash")
    assert envelope.components
