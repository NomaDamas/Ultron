"""Live model provider seam: typed LLM + VLM (multimodal) providers.

This module defines the model layer used by Ultron's generative UI:

* Shared primitives: ``ModelRole``, ``TextPart``, ``ImagePart``, ``ModelMessage``,
  ``ModelResponse``, ``ModelProviderConfig`` and the redacted ``SecretRef`` read model.
* Separate capability protocols: ``LlmProvider`` (text) and ``VlmProvider`` (vision),
  both backed by shared OpenAI-compatible HTTP helpers.
* Deterministic fakes used by default and in CI to exercise the real protocols
  without a network.

Safety invariants enforced here:

* Raw image bytes / data URLs / provider request payloads / authorization headers
  are request-scoped and never serialized. ``ImagePart`` exposes only safe metadata.
* Provider exceptions are sanitized into generic messages; the original cause is
  dropped so URLs, keys, and bodies never leak through ``__cause__`` or logs.
* Missing or partial live configuration fails closed via ``LiveModelUnavailable``.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ultron.ui.generator import LiveModelUnavailable


class LiveModelProviderError(RuntimeError):
    """Raised when a live provider request fails. Message is always sanitized."""


# ---------------------------------------------------------------------------
# Redacted secret read model (shared with config/settings stories).
# ---------------------------------------------------------------------------


class SecretRef(BaseModel):
    """Redacted view of a secret. Never carries the raw value."""

    configured: bool = False
    fingerprint: str | None = None
    last4: str | None = None
    source: str = "unset"  # env | dotenv | secret_store | unset
    updated_at: float | None = None

    @classmethod
    def from_secret(cls, raw: str | None, *, source: str, updated_at: float | None = None) -> "SecretRef":
        if not raw:
            return cls(configured=False, source="unset")
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        last4 = raw[-4:] if len(raw) >= 4 else None
        return cls(configured=True, fingerprint=fingerprint, last4=last4, source=source, updated_at=updated_at)


# ---------------------------------------------------------------------------
# Message primitives.
# ---------------------------------------------------------------------------


class ModelRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class TextPart:
    text: str
    kind: str = "text"


class RawImagePayload:
    """Non-serializable, request-scoped container for raw image bytes + data URL.

    This object must never be pickled, copied, JSON-serialized, or logged. It exists
    only during validation and the provider request, then is discarded.
    """

    __slots__ = ("_data", "_data_url")

    def __init__(self, data: bytes, data_url: str) -> None:
        self._data = data
        self._data_url = data_url

    @property
    def data(self) -> bytes:
        return self._data

    @property
    def data_url(self) -> str:
        return self._data_url

    def __reduce__(self) -> Any:  # pragma: no cover - defensive
        raise TypeError("RawImagePayload is not serializable")

    def __getstate__(self) -> Any:  # pragma: no cover - defensive
        raise TypeError("RawImagePayload is not serializable")

    def __deepcopy__(self, memo: Any) -> Any:  # pragma: no cover - defensive
        raise TypeError("RawImagePayload is not serializable")

    def __repr__(self) -> str:
        return "<RawImagePayload redacted>"


@dataclass
class ImagePart:
    """Image message part. Only metadata is durable/serializable.

    The raw bytes/data URL live on ``_raw`` (a ``RawImagePayload``) which is
    request-scoped and refuses serialization.
    """

    mime_type: str
    byte_length: int
    width: int
    height: int
    pixel_count: int
    fingerprint: str
    kind: str = "image"
    _raw: RawImagePayload | None = field(default=None, repr=False, compare=False)

    def metadata(self) -> dict[str, Any]:
        return {
            "kind": "image",
            "mime_type": self.mime_type,
            "byte_length": self.byte_length,
            "width": self.width,
            "height": self.height,
            "pixel_count": self.pixel_count,
            "fingerprint": self.fingerprint,
        }

    def provider_data_url(self) -> str:
        if self._raw is None:
            raise LiveModelUnavailable("image payload is not available for the provider request")
        return self._raw.data_url

    def discard_raw(self) -> None:
        """Drop the raw payload after the provider request completes."""
        self._raw = None


MessagePart = TextPart | ImagePart


@dataclass
class ModelMessage:
    role: ModelRole
    parts: list[MessagePart]

    def text(self) -> str:
        return "".join(part.text for part in self.parts if isinstance(part, TextPart))

    def has_image(self) -> bool:
        return any(isinstance(part, ImagePart) for part in self.parts)

    def safe_dump(self) -> dict[str, Any]:
        """Redacted, serialization-safe view: never includes raw image bytes/data URLs."""
        return {
            "role": self.role.value,
            "parts": [
                {"kind": "text", "text": part.text} if isinstance(part, TextPart) else part.metadata()
                for part in self.parts
            ],
        }

    def discard_raw(self) -> None:
        for part in self.parts:
            if isinstance(part, ImagePart):
                part.discard_raw()


class ModelResponse(BaseModel):
    text: str
    provider_name: str
    model_name: str
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float | None = None


# ---------------------------------------------------------------------------
# Provider configuration.
# ---------------------------------------------------------------------------


@dataclass
class ModelProviderConfig:
    base_url: str | None = None
    api_key: str | None = None
    model_name: str | None = None
    provider_kind: str = "openai-compatible"

    @property
    def is_complete(self) -> bool:
        return bool(self.base_url and self.api_key and self.model_name)

    def require(self, label: str) -> "ModelProviderConfig":
        if not self.is_complete:
            raise LiveModelUnavailable(
                f"live {label} requires base url, api key, and model name"
            )
        return self

    @classmethod
    def from_env(cls, role: str) -> "ModelProviderConfig":
        """Build config from process env.

        ``role`` is ``"llm"`` or ``"vlm"``. LLM falls back to the legacy
        ``ULTRON_MODEL_*`` variables for backward compatibility.
        """
        role_upper = role.upper()
        base = os.getenv(f"ULTRON_{role_upper}_BASE_URL")
        key = os.getenv(f"ULTRON_{role_upper}_API_KEY")
        model = os.getenv(f"ULTRON_{role_upper}_MODEL")
        if role == "llm":
            base = base or os.getenv("ULTRON_MODEL_BASE_URL")
            key = key or os.getenv("ULTRON_MODEL_API_KEY")
            model = model or os.getenv("ULTRON_MODEL_NAME")
        return cls(base_url=base, api_key=key, model_name=model)


# ---------------------------------------------------------------------------
# Capability protocols.
# ---------------------------------------------------------------------------


@runtime_checkable
class LlmProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def is_live(self) -> bool: ...

    def complete_text(self, messages: list[ModelMessage], schema_hint: str | None = None) -> ModelResponse: ...


@runtime_checkable
class VlmProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def is_live(self) -> bool: ...

    def complete_multimodal(self, messages: list[ModelMessage], schema_hint: str | None = None) -> ModelResponse: ...


# ---------------------------------------------------------------------------
# Shared OpenAI-compatible HTTP helper.
# ---------------------------------------------------------------------------


def _sanitize_provider_error() -> str:
    """Return a generic provider-error message; never echo url/key/body."""
    return "live model provider request failed"


def _post_chat_completions(
    config: ModelProviderConfig,
    messages_payload: list[dict[str, Any]],
    *,
    label: str,
    schema_hint: str | None,
) -> tuple[str, dict[str, Any]]:
    config.require(label)
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise LiveModelUnavailable("httpx is required for live model provider") from exc

    assert config.base_url is not None  # narrowed by require()
    url = config.base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"

    system_note = {
        "role": "system",
        "content": "Return only JSON matching the requested schema. Do not include markdown fences."
        + (f"\n\nSchema hint:\n{schema_hint}" if schema_hint else ""),
    }
    payload = {"model": config.model_name, "temperature": 0, "messages": [system_note, *messages_payload]}
    failed = False
    content = ""
    usage: dict[str, Any] = {}
    response = None
    data = None
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        content = str(data["choices"][0]["message"]["content"])
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
    except LiveModelUnavailable:
        raise
    except Exception:
        # Mark failure without retaining the caught exception so neither __cause__
        # nor __context__ can leak url/key/body/data URL.
        failed = True
    finally:
        # Ensure secret-bearing locals never outlive the request.
        response = None
        data = None
        payload = None
    if failed:
        # Raised outside any except handler: no __context__/__cause__ chain attaches.
        raise LiveModelProviderError(_sanitize_provider_error())
    return content, usage


# ---------------------------------------------------------------------------
# OpenAI-compatible live providers.
# ---------------------------------------------------------------------------


class OpenAICompatibleLlmProvider:
    """Text-only OpenAI-compatible chat completions provider."""

    def __init__(self, config: ModelProviderConfig | None = None) -> None:
        self.config = config or ModelProviderConfig.from_env("llm")

    @property
    def provider_id(self) -> str:
        return "openai-compatible-llm"

    @property
    def is_live(self) -> bool:
        return True

    def complete_text(self, messages: list[ModelMessage], schema_hint: str | None = None) -> ModelResponse:
        for message in messages:
            if message.has_image():
                raise LiveModelUnavailable("text-only LLM provider cannot accept image parts")
        payload = [{"role": m.role.value, "content": m.text()} for m in messages]
        content, usage = _post_chat_completions(self.config, payload, label="LLM provider", schema_hint=schema_hint)
        return ModelResponse(
            text=content,
            provider_name=self.provider_id,
            model_name=self.config.model_name or "",
            usage=usage,
        )


class OpenAICompatibleVlmProvider:
    """Multimodal OpenAI-compatible chat completions provider (text + image)."""

    def __init__(self, config: ModelProviderConfig | None = None) -> None:
        self.config = config or ModelProviderConfig.from_env("vlm")

    @property
    def provider_id(self) -> str:
        return "openai-compatible-vlm"

    @property
    def is_live(self) -> bool:
        return True

    def complete_multimodal(self, messages: list[ModelMessage], schema_hint: str | None = None) -> ModelResponse:
        payload: list[dict[str, Any]] = []
        try:
            for message in messages:
                content: list[dict[str, Any]] = []
                for part in message.parts:
                    if isinstance(part, TextPart):
                        content.append({"type": "text", "text": part.text})
                    elif isinstance(part, ImagePart):
                        content.append({"type": "image_url", "image_url": {"url": part.provider_data_url()}})
                payload.append({"role": message.role.value, "content": content})
            content_text, usage = _post_chat_completions(
                self.config, payload, label="VLM provider", schema_hint=schema_hint
            )
        finally:
            for message in messages:
                message.discard_raw()
        return ModelResponse(
            text=content_text,
            provider_name=self.provider_id,
            model_name=self.config.model_name or "",
            usage=usage,
        )


# ---------------------------------------------------------------------------
# Deterministic fakes (default + CI). Exercise the real protocols, no network.
# ---------------------------------------------------------------------------


class DeterministicFakeLlmProvider:
    """Fake LLM provider. Returns deterministic text via an injectable responder."""

    def __init__(self, responder: Any | None = None) -> None:
        self._responder = responder

    @property
    def provider_id(self) -> str:
        return "deterministic-fake-llm"

    @property
    def is_live(self) -> bool:
        return False

    def complete_text(self, messages: list[ModelMessage], schema_hint: str | None = None) -> ModelResponse:
        for message in messages:
            if message.has_image():
                raise LiveModelUnavailable("text-only LLM provider cannot accept image parts")
        if self._responder is not None:
            text = str(self._responder(messages, schema_hint))
        else:
            text = "\n".join(m.text() for m in messages).strip()
        return ModelResponse(text=text, provider_name=self.provider_id, model_name="fake-llm", usage={})


class DeterministicFakeVlmProvider:
    """Fake VLM provider. Returns a deterministic bounded observation."""

    def __init__(self, responder: Any | None = None) -> None:
        self._responder = responder

    @property
    def provider_id(self) -> str:
        return "deterministic-fake-vlm"

    @property
    def is_live(self) -> bool:
        return False

    def complete_multimodal(self, messages: list[ModelMessage], schema_hint: str | None = None) -> ModelResponse:
        try:
            if self._responder is not None:
                text = str(self._responder(messages, schema_hint))
            else:
                image_count = sum(1 for m in messages for p in m.parts if isinstance(p, ImagePart))
                text = f"observed {image_count} image(s)"
        finally:
            for message in messages:
                message.discard_raw()
        return ModelResponse(text=text, provider_name=self.provider_id, model_name="fake-vlm", usage={})


# ---------------------------------------------------------------------------
# Legacy text provider (kept for compatibility during migration).
# ---------------------------------------------------------------------------


class ModelProvider(Protocol):
    def complete(self, prompt: str, schema_hint: str | None) -> str: ...


class HttpModelProvider:
    """OpenAI-compatible chat completions provider, configured entirely by env.

    Legacy single-prompt seam used by ``LiveModelUiSpecGenerator`` and
    ``LiveModelModuleSynthesizer`` callsites. Wraps the typed LLM provider.
    """

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, model_name: str | None = None) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model_name = model_name

    @property
    def provider_id(self) -> str:
        return "http-openai-compatible"

    def _config(self) -> ModelProviderConfig:
        env = ModelProviderConfig.from_env("llm")
        return ModelProviderConfig(
            base_url=self.base_url or env.base_url,
            api_key=self.api_key or env.api_key,
            model_name=self.model_name or env.model_name,
        )

    def complete(self, prompt: str, schema_hint: str | None) -> str:
        config = self._config()
        if not config.is_complete:
            raise LiveModelUnavailable(
                "live model provider requires ULTRON_MODEL_BASE_URL, ULTRON_MODEL_API_KEY, and ULTRON_MODEL_NAME"
            )
        provider = OpenAICompatibleLlmProvider(config)
        message = ModelMessage(role=ModelRole.USER, parts=[TextPart(text=prompt)])
        return provider.complete_text([message], schema_hint).text
