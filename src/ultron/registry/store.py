"""Immutable in-memory registry for finalized harness modules."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ultron.module.model import HarnessModule, PersistencePolicy, PromotionState


class ModuleLifecycle(StrEnum):
    """Registry lifecycle states.

    Values intentionally mirror ``HarnessModule.fitness.promotion_state`` /
    ``PromotionState`` one-for-one: SEED, CANDIDATE, SURVIVOR, DECAYING,
    PRUNED, and QUARANTINED. Lifecycle is registry metadata, while
    PromotionState remains fitness metadata on the immutable module identity.
    """

    SEED = PromotionState.SEED.value
    CANDIDATE = PromotionState.CANDIDATE.value
    SURVIVOR = PromotionState.SURVIVOR.value
    DECAYING = PromotionState.DECAYING.value
    PRUNED = PromotionState.PRUNED.value
    QUARANTINED = PromotionState.QUARANTINED.value


Layer = Literal["global", "tenant", "user", "canary"]


class RegistryEntry(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    module: HarnessModule
    lifecycle: ModuleLifecycle
    layer: Layer
    created_at: float
    consent_ok: bool = False
    redacted: bool = False
    human_approved_additive: bool = False


class ModuleRegistry:
    """Content-addressed registry keyed by HarnessModule.content_hash."""

    def __init__(self) -> None:
        self._entries: dict[str, RegistryEntry] = {}

    def register(
        self,
        module: HarnessModule,
        lifecycle: ModuleLifecycle,
        layer: Layer,
        *,
        consent_ok: bool = False,
        redacted: bool = False,
        human_approved_additive: bool = False,
    ) -> RegistryEntry:
        if layer == "global" and not (consent_ok and redacted):
            raise ValueError("global modules require consent_ok=True and redacted=True")

        finalized = module.finalized()
        supplied_hash = module.content_hash or finalized.content_hash
        if supplied_hash is None:
            raise ValueError("finalized module must have content_hash")

        existing = self._entries.get(supplied_hash)
        if existing is not None:
            if _module_identity_bytes(existing.module) != _module_identity_bytes(finalized):
                raise ValueError("content hash collision: existing module bytes differ")
            return existing

        if supplied_hash != finalized.content_hash:
            raise ValueError("content hash does not match module identity bytes")

        entry = RegistryEntry(
            module=finalized,
            lifecycle=lifecycle,
            layer=layer,
            created_at=time.time(),
            consent_ok=consent_ok,
            redacted=redacted,
            human_approved_additive=human_approved_additive,
        )
        self._entries[finalized.content_hash] = entry
        return entry

    def get(self, content_hash: str) -> RegistryEntry:
        return self._entries[content_hash]

    def versions_of(self, module_id: str) -> list[RegistryEntry]:
        return sorted(
            (entry for entry in self._entries.values() if entry.module.module_id == module_id),
            key=lambda entry: (entry.module.version, entry.module.content_hash or ""),
        )

    def lineage(self, content_hash: str) -> list[RegistryEntry]:
        lineage: list[RegistryEntry] = []
        current = self.get(content_hash)
        while True:
            lineage.append(current)
            parent_hash = current.module.parent_id
            if parent_hash is None:
                return lineage
            current = self.get(parent_hash)

    def set_lifecycle(self, content_hash: str, new_lifecycle: ModuleLifecycle) -> RegistryEntry:
        existing = self.get(content_hash)
        updated = existing.model_copy(update={"lifecycle": new_lifecycle})
        self._entries[content_hash] = updated
        return updated

    def can_auto_promote(self, content_hash: str) -> bool:
        candidate = self.get(content_hash).module
        if candidate.parent_id is None:
            return True
        parent = self.get(candidate.parent_id).module
        return not _expands_permissions(candidate, parent)


def _module_identity_bytes(module: HarnessModule) -> bytes:
    finalized = module.finalized()
    return finalized.model_dump_json(
        include=set(HarnessModule.identity_fields()) | {"content_hash"},
        by_alias=False,
    ).encode("utf-8")


def _expands_permissions(candidate: HarnessModule, parent: HarnessModule) -> bool:
    if set(candidate.surfaces.tools) > set(parent.surfaces.tools):
        return True
    if _declared_surface_names(candidate) > _declared_surface_names(parent):
        return True
    if set(candidate.required_adapter_capabilities) > set(parent.required_adapter_capabilities):
        return True
    return _persistence_rank(candidate.persistence_policy) > _persistence_rank(parent.persistence_policy)


def _declared_surface_names(module: HarnessModule) -> set[str]:
    declared: set[str] = set()
    for name, value in module.surfaces.model_dump().items():
        if value not in (None, [], {}, False):
            declared.add(name)
    return declared


def _persistence_rank(policy: PersistencePolicy) -> int:
    return {
        PersistencePolicy.READ_ONLY: 0,
        PersistencePolicy.ISOLATED: 1,
        PersistencePolicy.CHECKPOINTED: 2,
        PersistencePolicy.NORMAL: 3,
    }[policy]
