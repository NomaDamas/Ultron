"""Story A: typed LLM + VLM provider layer and multimodal primitives."""

from __future__ import annotations

import copy
import json
import pickle
import sys
import types

import pytest

from ultron.model_provider import (
    DeterministicFakeLlmProvider,
    DeterministicFakeVlmProvider,
    HttpModelProvider,
    ImagePart,
    LiveModelProviderError,
    LlmProvider,
    ModelMessage,
    ModelProviderConfig,
    ModelResponse,
    ModelRole,
    OpenAICompatibleLlmProvider,
    OpenAICompatibleVlmProvider,
    RawImagePayload,
    SecretRef,
    TextPart,
    VlmProvider,
)
from ultron.ui.generator import LiveModelUnavailable


def _image_part(raw: bytes = b"\x89PNG\r\n\x1a\nfake") -> ImagePart:
    return ImagePart(
        mime_type="image/png",
        byte_length=len(raw),
        width=8,
        height=8,
        pixel_count=64,
        fingerprint="abc123",
        _raw=RawImagePayload(raw, "data:image/png;base64,ZmFrZQ=="),
    )


# ---------------------------------------------------------------------------
# Protocols + fakes
# ---------------------------------------------------------------------------


def test_fakes_satisfy_typed_protocols():
    assert isinstance(DeterministicFakeLlmProvider(), LlmProvider)
    assert isinstance(DeterministicFakeVlmProvider(), VlmProvider)
    assert isinstance(OpenAICompatibleLlmProvider(ModelProviderConfig()), LlmProvider)
    assert isinstance(OpenAICompatibleVlmProvider(ModelProviderConfig()), VlmProvider)


def test_fake_llm_returns_model_response():
    provider = DeterministicFakeLlmProvider(responder=lambda messages, hint: '{"ok": true}')
    out = provider.complete_text([ModelMessage(ModelRole.USER, [TextPart("hi")])])
    assert isinstance(out, ModelResponse)
    assert out.text == '{"ok": true}'
    assert provider.is_live is False


def test_fake_vlm_consumes_image_and_discards_raw():
    image = _image_part()
    msg = ModelMessage(ModelRole.USER, [TextPart("describe"), image])
    out = DeterministicFakeVlmProvider().complete_multimodal([msg])
    assert isinstance(out, ModelResponse)
    assert "image" in out.text
    # raw payload discarded after the provider call
    with pytest.raises(LiveModelUnavailable):
        image.provider_data_url()


# ---------------------------------------------------------------------------
# Non-serialization of raw image payloads
# ---------------------------------------------------------------------------


def test_raw_image_payload_not_serializable():
    raw = RawImagePayload(b"secretbytes", "data:image/png;base64,AAA")
    with pytest.raises(TypeError):
        pickle.dumps(raw)
    with pytest.raises(TypeError):
        copy.deepcopy(raw)
    assert "secretbytes" not in repr(raw)
    assert "AAA" not in repr(raw)


def test_image_part_safe_dump_excludes_raw():
    image = _image_part(b"rawsecretpixels")
    msg = ModelMessage(ModelRole.USER, [TextPart("look"), image])
    dump = msg.safe_dump()
    serialized = json.dumps(dump)
    assert "rawsecretpixels" not in serialized
    assert "data:image" not in serialized
    assert "base64" not in serialized
    # only safe metadata survives
    image_meta = dump["parts"][1]
    assert image_meta == {
        "kind": "image",
        "mime_type": "image/png",
        "byte_length": len(b"rawsecretpixels"),
        "width": 8,
        "height": 8,
        "pixel_count": 64,
        "fingerprint": "abc123",
    }


# ---------------------------------------------------------------------------
# Fail-closed config
# ---------------------------------------------------------------------------


def test_llm_provider_missing_config_fails_closed():
    provider = OpenAICompatibleLlmProvider(ModelProviderConfig())
    with pytest.raises(LiveModelUnavailable):
        provider.complete_text([ModelMessage(ModelRole.USER, [TextPart("hi")])])


def test_vlm_provider_missing_config_fails_closed():
    provider = OpenAICompatibleVlmProvider(ModelProviderConfig())
    with pytest.raises(LiveModelUnavailable):
        provider.complete_multimodal([ModelMessage(ModelRole.USER, [TextPart("hi"), _image_part()])])


def test_partial_config_is_not_complete():
    assert ModelProviderConfig(base_url="x", api_key="y").is_complete is False
    assert ModelProviderConfig(base_url="x", api_key="y", model_name="z").is_complete is True


def test_llm_rejects_image_parts():
    provider = OpenAICompatibleLlmProvider(ModelProviderConfig(base_url="u", api_key="k", model_name="m"))
    with pytest.raises(LiveModelUnavailable):
        provider.complete_text([ModelMessage(ModelRole.USER, [TextPart("x"), _image_part()])])
    # fake LLM also refuses images (text-only capability)
    with pytest.raises(LiveModelUnavailable):
        DeterministicFakeLlmProvider().complete_text([ModelMessage(ModelRole.USER, [_image_part()])])


# ---------------------------------------------------------------------------
# Provider exception sanitization
# ---------------------------------------------------------------------------


class _ExplodingHttpx(types.ModuleType):
    def __init__(self, secret: str):
        super().__init__("httpx")
        self.secret = secret

    def post(self, *args, **kwargs):  # noqa: ANN001
        raise RuntimeError(f"connection to https://secret.invalid with key {self.secret} failed")


def test_provider_exception_is_sanitized(monkeypatch):
    secret = "sk-supersecret-key-1234"
    monkeypatch.setitem(sys.modules, "httpx", _ExplodingHttpx(secret))
    provider = OpenAICompatibleLlmProvider(
        ModelProviderConfig(base_url="https://secret.invalid/v1", api_key=secret, model_name="m")
    )
    with pytest.raises(LiveModelProviderError) as excinfo:
        provider.complete_text([ModelMessage(ModelRole.USER, [TextPart("hi")])])
    message = str(excinfo.value)
    assert secret not in message
    assert "secret.invalid" not in message
    # No exception chain (neither __cause__ nor __context__) retains url/key/body.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_vlm_provider_exception_is_sanitized(monkeypatch):
    secret = "sk-vlm-secret-5678"
    data_url = "data:image/png;base64,RAWIMAGEPAYLOAD"

    class _VlmExploder(types.ModuleType):
        def __init__(self):
            super().__init__("httpx")

        def post(self, *args, **kwargs):  # noqa: ANN001
            raise RuntimeError(f"provider body leaked {secret} and {data_url}")

    monkeypatch.setitem(sys.modules, "httpx", _VlmExploder())
    image = ImagePart(
        mime_type="image/png", byte_length=4, width=8, height=8, pixel_count=64,
        fingerprint="fp", _raw=RawImagePayload(b"raw", data_url),
    )
    provider = OpenAICompatibleVlmProvider(
        ModelProviderConfig(base_url="https://vlm.invalid/v1", api_key=secret, model_name="m")
    )
    with pytest.raises(LiveModelProviderError) as excinfo:
        provider.complete_multimodal([ModelMessage(ModelRole.USER, [TextPart("look"), image])])
    blob = repr(excinfo.value) + str(excinfo.value)
    assert secret not in blob
    assert data_url not in blob
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    # raw payload discarded even on failure
    with pytest.raises(LiveModelUnavailable):
        image.provider_data_url()


class _BadJsonHttpx(types.ModuleType):
    def __init__(self):
        super().__init__("httpx")

    def post(self, *args, **kwargs):  # noqa: ANN001
        module = self

        class _Resp:
            def raise_for_status(self_inner):
                return None

            def json(self_inner):
                return {"unexpected": "shape"}

        return _Resp()


def test_invalid_provider_response_is_sanitized(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", _BadJsonHttpx())
    provider = OpenAICompatibleLlmProvider(ModelProviderConfig(base_url="u", api_key="k", model_name="m"))
    with pytest.raises(LiveModelProviderError):
        provider.complete_text([ModelMessage(ModelRole.USER, [TextPart("hi")])])


# ---------------------------------------------------------------------------
# SecretRef redaction
# ---------------------------------------------------------------------------


def test_secret_ref_redacts():
    ref = SecretRef.from_secret("sk-abcdef123456", source="secret_store")
    assert ref.configured is True
    assert ref.last4 == "3456"
    assert ref.source == "secret_store"
    dumped = json.dumps(ref.model_dump())
    assert "sk-abcdef123456" not in dumped
    assert "abcdef" not in dumped  # only a sha256 prefix fingerprint, not raw chars

    empty = SecretRef.from_secret(None, source="env")
    assert empty.configured is False
    assert empty.source == "unset"


# ---------------------------------------------------------------------------
# Legacy provider compatibility
# ---------------------------------------------------------------------------


def test_legacy_http_provider_missing_env_fails_closed(monkeypatch):
    for key in ["ULTRON_MODEL_BASE_URL", "ULTRON_MODEL_API_KEY", "ULTRON_MODEL_NAME",
                "ULTRON_LLM_BASE_URL", "ULTRON_LLM_API_KEY", "ULTRON_LLM_MODEL"]:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(LiveModelUnavailable, match="ULTRON_MODEL_BASE_URL"):
        HttpModelProvider().complete("prompt", None)


def test_env_fallback_for_llm_config(monkeypatch):
    for key in ["ULTRON_LLM_BASE_URL", "ULTRON_LLM_API_KEY", "ULTRON_LLM_MODEL"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ULTRON_MODEL_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("ULTRON_MODEL_API_KEY", "k")
    monkeypatch.setenv("ULTRON_MODEL_NAME", "m")
    config = ModelProviderConfig.from_env("llm")
    assert config.is_complete
    assert config.base_url == "https://llm.example/v1"
