"""Deterministic composition manifest models."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SurfaceConflict(BaseModel):
    surface: str
    kind: str
    winner_hash: str | None
    losers: list[str] = Field(default_factory=list)
    rationale: str


class ModuleSetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_scope: str
    workflow_fingerprint: str
    request_class: str
    ordered_module_hashes: list[str]
    resolved_prompt_order: list[str]
    resolved_tool_allowlist: list[str]
    resolved_ui_panels: list[str]
    disabled_modules: list[str]
    conflicts: list[SurfaceConflict]
    safety_policy: dict[str, Any]
    budget_policy: dict[str, Any]
    rationale: str
    manifest_hash: str | None = None

    def compute_manifest_hash(self) -> str:
        content = self.model_dump(mode="json", exclude={"manifest_hash"})
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def finalized(self) -> "ModuleSetManifest":
        return self.model_copy(update={"manifest_hash": self.compute_manifest_hash()})
