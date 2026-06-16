"""Story C: model-driven UiSpec/synthesis plus bounded image-input pipeline."""

from __future__ import annotations

import base64
import io
import json

import pytest

from ultron.images import (
    MAX_BYTES,
    MAX_DIMENSION,
    ImageRejected,
    validate_image,
)
from ultron.model_provider import (
    DeterministicFakeLlmProvider,
    DeterministicFakeVlmProvider,
    ImagePart,
    ModelMessage,
    ModelRole,
    TextPart,
)
from ultron.ui.generator import LiveModelUiSpecGenerator, LiveModelUnavailable, UiGenContext

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

try:
    from fastapi.testclient import TestClient

    from ultron.app.server import create_app
except Exception:  # pragma: no cover
    TestClient = None  # type: ignore


def _png_bytes(width=8, height=8, color=(10, 20, 30), with_exif=False) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    if with_exif:
        exif = img.getexif()
        exif[0x010E] = "SECRET-EXIF-DESCRIPTION"  # ImageDescription
        img.save(buf, format="JPEG", exif=exif)
    else:
        img.save(buf, format="PNG")
    return buf.getvalue()


pytestmark = pytest.mark.skipif(Image is None, reason="Pillow unavailable")


# ---------------------------------------------------------------------------
# Image pipeline
# ---------------------------------------------------------------------------


def test_valid_png_produces_image_part():
    part = validate_image(_png_bytes())
    assert isinstance(part, ImagePart)
    assert part.mime_type == "image/png"
    assert part.width == 8 and part.height == 8
    assert part.pixel_count == 64
    assert part.provider_data_url().startswith("data:image/png;base64,")


def test_oversized_bytes_rejected():
    with pytest.raises(ImageRejected) as e:
        validate_image(b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_BYTES + 1))
    assert "too large" in str(e.value)


def test_unsupported_type_rejected():
    with pytest.raises(ImageRejected) as e:
        validate_image(b"GIF89a" + b"0" * 100)
    assert str(e.value) == "unsupported image type"


def test_oversized_dimensions_rejected():
    big = _png_bytes(width=MAX_DIMENSION + 1, height=2)
    with pytest.raises(ImageRejected) as e:
        validate_image(big)
    assert "dimensions too large" in str(e.value)


def test_truncated_image_rejected():
    raw = _png_bytes()
    with pytest.raises(ImageRejected):
        validate_image(raw[: len(raw) // 2])


def test_exif_is_stripped():
    jpeg = _png_bytes(with_exif=True)
    assert b"SECRET-EXIF-DESCRIPTION" in jpeg  # present before sanitization
    part = validate_image(jpeg)
    # the sanitized provider payload must not carry the EXIF description
    decoded = base64.b64decode(part.provider_data_url().split(",", 1)[1])
    assert b"SECRET-EXIF-DESCRIPTION" not in decoded


# ---------------------------------------------------------------------------
# Model-driven generation through fake providers
# ---------------------------------------------------------------------------


class _FakeManifest:
    resolved_ui_panels = ["plan", "risk"]


def _valid_uispec_json() -> str:
    return json.dumps(
        {
            "components": [
                {"type": "PLAN_PANEL", "region": "main", "priority": 1, "props": {}, "telemetry_schema": []}
            ]
        }
    )


def test_fake_llm_drives_uispec_generator():
    captured = {}

    def responder(messages, hint):
        captured["prompt"] = messages[0].text()
        captured["hint"] = hint
        return _valid_uispec_json()

    gen = LiveModelUiSpecGenerator(DeterministicFakeLlmProvider(responder=responder))
    from ultron.ui.runtime import ComponentType

    spec = gen.generate(
        UiGenContext(
            module_set_manifest=_FakeManifest(),
            request_class="triage",
            allowed_registry=list(ComponentType),
            request_image_metadata=[{"mime_type": "image/png", "width": 8, "height": 8}],
            vlm_observations=["a bounded observation"],
        )
    )
    assert spec.spec_hash is not None
    assert spec.components[0].type.value == "PLAN_PANEL"
    # the model prompt carried image metadata + vlm observation as context
    assert "vlm_observations" in captured["prompt"]
    assert "request_image_metadata" in captured["prompt"]


def test_model_generator_invalid_json_fails_closed():
    gen = LiveModelUiSpecGenerator(DeterministicFakeLlmProvider(responder=lambda m, h: "not json"))
    from ultron.ui.runtime import ComponentType

    with pytest.raises(ValueError):
        gen.generate(UiGenContext(module_set_manifest=_FakeManifest(), request_class="triage", allowed_registry=list(ComponentType)))


def test_model_generator_no_provider_fails_closed():
    gen = LiveModelUiSpecGenerator(None)
    with pytest.raises(LiveModelUnavailable):
        gen.generate(UiGenContext(module_set_manifest=_FakeManifest(), request_class="triage"))


# ---------------------------------------------------------------------------
# VLM observation is bounded, redacted, context-only
# ---------------------------------------------------------------------------


def test_observe_images_redacts_and_is_bounded():
    from ultron.app.triage import build_triage_app_from_env

    engine = build_triage_app_from_env()
    secret_request = "analyze this API_KEY=sk-supersecret-abc123"
    part = validate_image(_png_bytes())

    def responder(messages, hint):
        # a malicious VLM echoing the secret back
        return "I see API_KEY=sk-supersecret-abc123 and <script>alert(1)</script>"

    engine.vlm_provider = DeterministicFakeVlmProvider(responder=responder)
    metadata, observations = engine.observe_images([part], secret_request)
    assert metadata[0]["mime_type"] == "image/png"
    assert len(observations) == 1
    obs = observations[0]
    assert "sk-supersecret-abc123" not in obs
    assert "<script>" not in obs  # angle brackets neutralized
    assert len(obs) <= 240


# ---------------------------------------------------------------------------
# Server end-to-end image submit
# ---------------------------------------------------------------------------


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_submit_with_valid_image(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    client = TestClient(create_app())
    csrf = client.get("/").cookies["ultron_csrf"]
    image_b64 = base64.b64encode(_png_bytes()).decode("ascii")
    resp = client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "look at this", "image_base64": image_b64}, "csrf_token": csrf},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    # the raw base64 never appears in the response envelope
    assert image_b64 not in resp.text


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_submit_with_oversized_image_safe_error(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    client = TestClient(create_app())
    csrf = client.get("/").cookies["ultron_csrf"]
    oversized = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_BYTES + 1)).decode("ascii")
    resp = client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "x", "image_base64": oversized}, "csrf_token": csrf},
    )
    assert resp.status_code == 422
    assert "too large" in resp.text


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_submit_with_garbage_image_safe_error(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    client = TestClient(create_app())
    csrf = client.get("/").cookies["ultron_csrf"]
    resp = client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "x", "image_base64": "@@not-base64@@"}, "csrf_token": csrf},
    )
    assert resp.status_code == 422
    assert "validation failed" in resp.text
