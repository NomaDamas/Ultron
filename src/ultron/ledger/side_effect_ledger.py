"""Append-only side-effect ledger for attributed canary state."""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SideEffectKind(StrEnum):
    POINTER_TRANSITION = "POINTER_TRANSITION"
    CANDIDATE_LIFECYCLE = "CANDIDATE_LIFECYCLE"
    UISPEC_CACHE = "UISPEC_CACHE"
    ADAPTER_STATE = "ADAPTER_STATE"
    HERMES_MEMORY = "HERMES_MEMORY"
    HERMES_SKILL = "HERMES_SKILL"
    WORKSPACE_PATCH = "WORKSPACE_PATCH"
    EXTERNAL_CALL = "EXTERNAL_CALL"
    FEEDBACK_EVENT = "FEEDBACK_EVENT"
    TELEMETRY = "TELEMETRY"


class LedgerEntry(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    entry_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    module_set_hash: str
    module_hash: str | None = None
    canary_id: str | None = None
    kind: SideEffectKind
    payload: dict[str, Any] = Field(default_factory=dict)
    reversible: bool = True
    non_reversible_marker: str | None = None
    created_at: float = Field(default_factory=time.time)
    quarantined: bool = False


class SideEffectLedger:
    """In-memory append-only audit log; quarantine mutates flags without deletion."""

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []

    def append(self, entry: LedgerEntry) -> str:
        stored = entry.model_copy(deep=True)
        self._entries.append(stored)
        return stored.entry_id

    def entries_for_canary(self, canary_id: str) -> list[LedgerEntry]:
        return [entry.model_copy(deep=True) for entry in self._entries if entry.canary_id == canary_id]

    def entries_for_run(self, run_id: str) -> list[LedgerEntry]:
        return [entry.model_copy(deep=True) for entry in self._entries if entry.run_id == run_id]

    def mark_quarantined(self, canary_id: str) -> list[str]:
        quarantined: list[str] = []
        for index, entry in enumerate(self._entries):
            if entry.canary_id == canary_id:
                updated = entry.model_copy(update={"quarantined": True}, deep=True)
                self._entries[index] = updated
                quarantined.append(updated.entry_id)
        return quarantined

    def promotable_entries(self) -> list[LedgerEntry]:
        return [entry.model_copy(deep=True) for entry in self._entries if not entry.quarantined]
