"""Content-addressed module artifact blobs."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field


class BlobKind(StrEnum):
    PROMPT_PACK = "PROMPT_PACK"
    TOOL_POLICY = "TOOL_POLICY"
    UI_PANEL_CONTRACT = "UI_PANEL_CONTRACT"
    SAFETY_POLICY = "SAFETY_POLICY"
    BUDGET_POLICY = "BUDGET_POLICY"


class _CanonicalBlob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def canonical_bytes(self) -> bytes:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        return canonical.encode("utf-8")

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class PromptPack(_CanonicalBlob):
    slots: dict[str, str]
    notes: str = ""


class ToolPolicyBlob(_CanonicalBlob):
    tools: list[str]
    rationale: str = ""


class UiPanelContract(_CanonicalBlob):
    panels: list[str]
    notes: str = ""


class SafetyPolicyBlob(_CanonicalBlob):
    workspace_writes: bool = False
    external_calls: bool = False
    extra_rules: dict[str, Any] = Field(default_factory=dict)


class BudgetPolicyBlob(_CanonicalBlob):
    max_tool_calls: int
    max_cost: float | None = None
    max_latency_s: float | None = None


BlobT = TypeVar("BlobT", bound=_CanonicalBlob)
ModuleBlob = PromptPack | ToolPolicyBlob | UiPanelContract | SafetyPolicyBlob | BudgetPolicyBlob


class BlobStore:
    """In-memory content-addressed blob store keyed by kind and sha256 content hash."""

    def __init__(self) -> None:
        self._blobs: dict[tuple[BlobKind, str], ModuleBlob] = {}

    def put(self, kind: BlobKind, blob: ModuleBlob) -> str:
        content_hash = blob.content_hash()
        key = (kind, content_hash)
        existing = self._blobs.get(key)
        if existing is not None and existing.canonical_bytes() != blob.canonical_bytes():
            raise ValueError(f"blob content hash collision for {kind.value}: {content_hash}")
        self._blobs[key] = blob.model_copy(deep=True)
        return content_hash

    def get(self, kind: BlobKind, content_hash: str) -> ModuleBlob:
        return self._blobs[(kind, content_hash)].model_copy(deep=True)

    def get_typed(self, kind: BlobKind, content_hash: str, expected_type: type[BlobT]) -> BlobT:
        blob = self.get(kind, content_hash)
        if not isinstance(blob, expected_type):
            raise TypeError(f"blob {content_hash} for {kind.value} is not {expected_type.__name__}")
        return cast(BlobT, blob)

    def has(self, kind: BlobKind, content_hash: str) -> bool:
        return (kind, content_hash) in self._blobs
