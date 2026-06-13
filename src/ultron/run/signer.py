"""Run manifest HMAC signing with explicit key providers."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Mapping, Protocol, Any


def _canonical_json(payload: dict[str, Any]) -> str:
    import json
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class KeyProvider(Protocol):
    def resolve(self, key_id: str) -> str: ...


class FixtureKeyProvider:
    def __init__(self, keys: Mapping[str, str]) -> None:
        self.keys = dict(keys)

    def resolve(self, key_id: str) -> str:
        try:
            return self.keys[key_id]
        except KeyError as exc:
            raise KeyError(f"missing fixture manifest signing key: {key_id}") from exc


class EnvKeyProvider:
    def __init__(self, env_var: str = "ULTRON_RUN_MANIFEST_SIGNING_SECRET") -> None:
        self.env_var = env_var

    def resolve(self, key_id: str) -> str:
        secret = os.environ.get(self.env_var)
        if not secret:
            raise RuntimeError(f"missing run manifest signing secret in {self.env_var}")
        return secret


class ManifestSigner:
    def __init__(self, key_id: str, secret: str) -> None:
        if not key_id:
            raise ValueError("key_id is required")
        if not secret:
            raise ValueError("secret is required")
        self.key_id = key_id
        self.secret = secret

    @classmethod
    def from_provider(cls, key_id: str, provider: KeyProvider) -> "ManifestSigner":
        return cls(key_id, provider.resolve(key_id))

    def sign(self, payload: dict[str, Any]) -> str:
        return hmac.new(self.secret.encode("utf-8"), _canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, payload: dict[str, Any], signature: str, key_id: str) -> bool:
        if key_id != self.key_id:
            return False
        expected = self.sign(payload)
        return hmac.compare_digest(signature, expected)
