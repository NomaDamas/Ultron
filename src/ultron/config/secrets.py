"""Out-of-repo, owner-only local secret store for Ultron runtime config.

The store is a plain JSON file under ``${ULTRON_CONFIG_DIR:-~/.config/ultron}/secrets.json``.
It is created with owner-only permissions where the platform supports it, is never
served statically, never committed, and never copied into ``.gjc`` artifacts.

The redacted read model lives in ``ultron.model_provider.SecretRef``; this module only
stores and returns raw values to server-side / request-scoped callers. A keyring or
SQLite backend can replace it later without changing the redacted API contract.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def default_config_dir() -> Path:
    override = os.getenv("ULTRON_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "ultron"


def default_store_path() -> Path:
    return default_config_dir() / "secrets.json"


class SecretStore:
    """Owner-only JSON-backed secret store keyed by dotted config keys."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_store_path()

    # -- internal io ------------------------------------------------------

    def _read_all(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write_all(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        # Write with a private umask, then tighten permissions.
        fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
        finally:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    # -- public api -------------------------------------------------------

    def get_value(self, key: str) -> str | None:
        entry = self._read_all().get(key)
        if isinstance(entry, dict):
            value = entry.get("value")
            return str(value) if value is not None else None
        return str(entry) if entry is not None else None

    def get_updated_at(self, key: str) -> float | None:
        entry = self._read_all().get(key)
        if isinstance(entry, dict) and entry.get("updated_at") is not None:
            try:
                return float(entry["updated_at"])
            except (TypeError, ValueError):
                return None
        return None

    def set_value(self, key: str, value: str) -> None:
        data = self._read_all()
        data[key] = {"value": value, "updated_at": time.time()}
        self._write_all(data)

    def delete_value(self, key: str) -> None:
        data = self._read_all()
        if key in data:
            del data[key]
            self._write_all(data)

    def keys(self) -> list[str]:
        return sorted(self._read_all().keys())
