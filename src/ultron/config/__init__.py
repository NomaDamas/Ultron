"""Runtime configuration: keys, ``.env`` loading, layered resolution, settings models.

Precedence (highest first): runtime secret store > process env > ``.env`` > unset.

* Non-secret keys (base urls, model names, provider kind) and secret keys
  (``llm.api_key``, ``vlm.api_key``) share one dotted-key namespace.
* Reads expose only ``ModelSettingsRead`` with redacted ``SecretRef`` fields;
  raw secrets are returned only to server-side / request-scoped callers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from ultron.model_provider import ModelProviderConfig, SecretRef
from ultron.config.secrets import SecretStore

SECRET_KEYS: frozenset[str] = frozenset({"llm.api_key", "vlm.api_key"})
NON_SECRET_KEYS: frozenset[str] = frozenset(
    {"llm.base_url", "llm.model", "vlm.base_url", "vlm.model", "provider_kind"}
)
ALL_KEYS: frozenset[str] = SECRET_KEYS | NON_SECRET_KEYS

# Each config key maps to env var candidates (first match wins). LLM keys fall back
# to the legacy ULTRON_MODEL_* names.
ENV_MAP: dict[str, tuple[str, ...]] = {
    "llm.base_url": ("ULTRON_LLM_BASE_URL", "ULTRON_MODEL_BASE_URL"),
    "llm.api_key": ("ULTRON_LLM_API_KEY", "ULTRON_MODEL_API_KEY"),
    "llm.model": ("ULTRON_LLM_MODEL", "ULTRON_MODEL_NAME"),
    "vlm.base_url": ("ULTRON_VLM_BASE_URL",),
    "vlm.api_key": ("ULTRON_VLM_API_KEY",),
    "vlm.model": ("ULTRON_VLM_MODEL",),
    "provider_kind": ("ULTRON_PROVIDER_KIND",),
}


def is_secret_key(key: str) -> bool:
    return key in SECRET_KEYS


# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------


def parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :]
        if "=" not in stripped:
            continue
        name, _, raw = stripped.partition("=")
        name = name.strip()
        raw = raw.strip()
        if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
            raw = raw[1:-1]
        if name:
            values[name] = raw
    return values


def load_dotenv(path: str | Path = ".env", *, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Load ``.env`` into ``environ`` without overriding existing values.

    Process env always wins over ``.env``. Returns the parsed dotenv mapping so
    callers can attribute a value's source.
    """
    env = environ if environ is not None else os.environ
    dotenv_path = Path(path)
    if not dotenv_path.is_file():
        return {}
    parsed = parse_dotenv(dotenv_path.read_text(encoding="utf-8"))
    for name, value in parsed.items():
        env.setdefault(name, value)
    return parsed


# ---------------------------------------------------------------------------
# Settings read/write models
# ---------------------------------------------------------------------------


class ModelSettingsRead(BaseModel):
    llm_configured: bool = False
    vlm_configured: bool = False
    provider_kind: str = "openai-compatible"
    llm_model: str | None = None
    vlm_model: str | None = None
    llm_base_url_label: str | None = None
    vlm_base_url_label: str | None = None
    llm_api_key: SecretRef = Field(default_factory=SecretRef)
    vlm_api_key: SecretRef = Field(default_factory=SecretRef)
    last_validation_status: str | None = None


class ModelSettingsWrite(BaseModel):
    """Write-only settings payload. Never echoed back to clients."""

    model_config = ConfigDict(extra="forbid")

    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    vlm_base_url: str | None = None
    vlm_model: str | None = None
    vlm_api_key: str | None = None
    provider_kind: str | None = None

    def as_key_values(self) -> list[tuple[str, str]]:
        mapping = {
            "llm.base_url": self.llm_base_url,
            "llm.model": self.llm_model,
            "llm.api_key": self.llm_api_key,
            "vlm.base_url": self.vlm_base_url,
            "vlm.model": self.vlm_model,
            "vlm.api_key": self.vlm_api_key,
            "provider_kind": self.provider_kind,
        }
        return [(key, value) for key, value in mapping.items() if value is not None and value != ""]


def _base_url_label(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value if "//" in value else f"//{value}")
    return parsed.hostname or None

def host_label(value: str | None) -> str | None:
    """Public alias for redacting a base URL to its host."""
    return _base_url_label(value)


# ---------------------------------------------------------------------------
# Config service
# ---------------------------------------------------------------------------


class ConfigService:
    """Resolves config across secret store, process env, and ``.env``."""

    def __init__(
        self,
        *,
        store: SecretStore | None = None,
        environ: dict[str, str] | None = None,
        dotenv: dict[str, str] | None = None,
    ) -> None:
        self.store = store if store is not None else SecretStore()
        self._environ = environ if environ is not None else os.environ
        self._dotenv = dotenv or {}
        self.audit: list[dict[str, object]] = []

    def _env_value(self, key: str) -> str | None:
        for candidate in ENV_MAP.get(key, ()):  # process env wins
            if candidate in self._environ and self._environ[candidate] != "":
                return self._environ[candidate]
        return None

    def _dotenv_value(self, key: str) -> str | None:
        for candidate in ENV_MAP.get(key, ()):
            if candidate in self._dotenv and self._dotenv[candidate] != "":
                return self._dotenv[candidate]
        return None

    def resolve(self, key: str) -> tuple[str | None, str]:
        """Return ``(value, source)`` with secret_store > env > dotenv precedence."""
        store_value = self.store.get_value(key)
        if store_value not in (None, ""):
            return store_value, "secret_store"
        env_value = self._env_value(key)
        if env_value not in (None, ""):
            return env_value, "env"
        dotenv_value = self._dotenv_value(key)
        if dotenv_value not in (None, ""):
            return dotenv_value, "dotenv"
        return None, "unset"

    def value(self, key: str) -> str | None:
        return self.resolve(key)[0]

    def secret_ref(self, key: str) -> SecretRef:
        raw, source = self.resolve(key)
        updated_at = self.store.get_updated_at(key) if source == "secret_store" else None
        return SecretRef.from_secret(raw, source=source, updated_at=updated_at)

    def provider_config(self, role: str) -> ModelProviderConfig:
        return ModelProviderConfig(
            base_url=self.value(f"{role}.base_url"),
            api_key=self.value(f"{role}.api_key"),
            model_name=self.value(f"{role}.model"),
            provider_kind=self.value("provider_kind") or "openai-compatible",
        )

    def model_settings_read(self) -> ModelSettingsRead:
        llm = self.provider_config("llm")
        vlm = self.provider_config("vlm")
        return ModelSettingsRead(
            llm_configured=llm.is_complete,
            vlm_configured=vlm.is_complete,
            provider_kind=self.value("provider_kind") or "openai-compatible",
            llm_model=self.value("llm.model"),
            vlm_model=self.value("vlm.model"),
            llm_base_url_label=_base_url_label(self.value("llm.base_url")),
            vlm_base_url_label=_base_url_label(self.value("vlm.base_url")),
            llm_api_key=self.secret_ref("llm.api_key"),
            vlm_api_key=self.secret_ref("vlm.api_key"),
            last_validation_status=None,
        )

    def set(self, key: str, value: str, *, actor: str | None = None) -> None:
        if key not in ALL_KEYS:
            raise KeyError("unknown config key")
        self.store.set_value(key, value)
        self._audit(actor, [key])

    def apply_write(self, payload: ModelSettingsWrite, *, actor: str | None = None) -> ModelSettingsRead:
        changed: list[str] = []
        for key, value in payload.as_key_values():
            if key not in ALL_KEYS:
                raise KeyError("unknown config key")
            self.store.set_value(key, value)
            changed.append(key)
        if changed:
            self._audit(actor, changed)
        return self.model_settings_read()

    def _audit(self, actor: str | None, keys: Iterable[str]) -> None:
        entry = {
            "actor": actor or "unknown",
            "fields": sorted(keys),
            "fingerprints": {key: self.secret_ref(key).fingerprint for key in keys if is_secret_key(key)},
        }
        self.audit.append(entry)


def build_config_service() -> ConfigService:
    """Build a ConfigService, loading ``.env`` into process env first.

    A snapshot of the process env taken BEFORE the dotenv merge is handed to the
    ConfigService so source attribution stays correct (``env`` vs ``dotenv``),
    while os.environ is still populated for legacy ``os.getenv`` consumers.
    """
    pre_env = dict(os.environ)
    dotenv = load_dotenv(os.getenv("ULTRON_DOTENV_PATH", ".env"))
    return ConfigService(environ=pre_env, dotenv=dotenv)
