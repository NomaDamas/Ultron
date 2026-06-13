"""Immutable in-memory registry for finalized harness modules."""

from __future__ import annotations

import time
import string

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from ultron.module.blobs import BlobStore


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

    def __init__(self, blob_store: BlobStore | None = None, *, allow_unbacked_refs: bool = False) -> None:
        self._entries: dict[str, RegistryEntry] = {}
        self._registration_returns: dict[str, RegistryEntry] = {}
        self.blob_store = blob_store
        self.allow_unbacked_refs = allow_unbacked_refs


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

        self._verify_blob_references(module)

        finalized = module.finalized()
        supplied_hash = module.content_hash or finalized.content_hash
        if supplied_hash is None:
            raise ValueError("finalized module must have content_hash")

        existing = self._entries.get(supplied_hash)
        if existing is not None:
            if _module_identity_bytes(existing.module) != _module_identity_bytes(finalized):
                raise ValueError("content hash collision: existing module bytes differ")
            return self._registration_returns[supplied_hash]

        if supplied_hash != finalized.content_hash:
            raise ValueError("content hash does not match module identity bytes")

        entry = RegistryEntry(
            module=finalized.model_copy(deep=True),
            lifecycle=lifecycle,
            layer=layer,
            created_at=time.time(),
            consent_ok=consent_ok,
            redacted=redacted,
            human_approved_additive=human_approved_additive,
        )
        stored_entry = entry.model_copy(deep=True)
        self._entries[finalized.content_hash] = stored_entry
        returned_entry = stored_entry.model_copy(deep=True)
        self._registration_returns[finalized.content_hash] = returned_entry
        return returned_entry

    def get(self, content_hash: str) -> RegistryEntry:
        return self._entries[content_hash].model_copy(deep=True)

    def versions_of(self, module_id: str) -> list[RegistryEntry]:
        entries = sorted(
            (entry for entry in self._entries.values() if entry.module.module_id == module_id),
            key=lambda entry: (entry.module.version, entry.module.content_hash or ""),
        )
        return [entry.model_copy(deep=True) for entry in entries]

    def lineage(self, content_hash: str) -> list[RegistryEntry]:
        lineage: list[RegistryEntry] = []
        current = self._entries[content_hash]
        while True:
            lineage.append(current.model_copy(deep=True))
            parent_hash = current.module.parent_id
            if parent_hash is None:
                return lineage
            current = self._entries[parent_hash]

    def set_lifecycle(self, content_hash: str, new_lifecycle: ModuleLifecycle) -> RegistryEntry:
        existing = self._entries[content_hash]
        updated = existing.model_copy(update={"lifecycle": new_lifecycle}, deep=True)
        self._entries[content_hash] = updated
        self._registration_returns[content_hash] = updated.model_copy(deep=True)
        return updated.model_copy(deep=True)

    def _verify_blob_references(self, module: HarnessModule) -> None:
        if self.blob_store is None:
            return
        for kind, content_hash in module.referenced_blob_hashes().items():
            if content_hash is None:
                continue
            if not _is_sha256_hex(content_hash):
                if self.allow_unbacked_refs:
                    continue
                raise ValueError(f"artifact ref not blob-backed for {kind.value}: {content_hash}")

            if not self.blob_store.has(kind, content_hash):
                raise ValueError(f"missing blob for {kind.value}: {content_hash}")
            stored = self.blob_store.get(kind, content_hash)
            actual_hash = stored.content_hash()
            if actual_hash != content_hash:
                raise ValueError(f"blob hash mismatch for {kind.value}: expected {content_hash}, got {actual_hash}")

    def can_auto_promote(self, content_hash_or_module: str | HarnessModule) -> bool:
        if isinstance(content_hash_or_module, HarnessModule):
            candidate = content_hash_or_module.finalized()
        else:
            candidate = self._entries[content_hash_or_module].module
        if candidate.parent_id is None:
            return True
        parent = self._entries[candidate.parent_id].module
        return not _expands_permissions(candidate, parent)


def _module_identity_bytes(module: HarnessModule) -> bytes:
    finalized = module.finalized()
    return finalized.model_dump_json(
        include=set(HarnessModule.identity_fields()) | {"content_hash"},
        by_alias=False,
    ).encode("utf-8")


def expands_module_permissions(candidate: HarnessModule, parent: HarnessModule) -> bool:
    """Return True when candidate widens any permission-bearing module surface."""
    if not _list_subset(candidate.surfaces.tools, parent.surfaces.tools):
        return True
    if not _list_subset(candidate.surfaces.ui_panels, parent.surfaces.ui_panels):
        return True
    if not _list_subset(candidate.surfaces.skill_refs, parent.surfaces.skill_refs):
        return True
    if not _list_subset(candidate.skill_refs, parent.skill_refs):
        return True
    if candidate.surfaces.topology_fragment and candidate.surfaces.topology_fragment != parent.surfaces.topology_fragment:
        return True
    if not _list_subset(candidate.required_adapter_capabilities, parent.required_adapter_capabilities):
        return True
    if _persistence_rank(candidate.persistence_policy) > _persistence_rank(parent.persistence_policy):
        return True
    if _dict_widens(candidate.surfaces.persistence, parent.surfaces.persistence):
        return True
    if _safety_widens(candidate.surfaces.safety, parent.surfaces.safety):
        return True
    return _budget_widens(candidate.surfaces.budget, parent.surfaces.budget)


_expands_permissions = expands_module_permissions


def _list_subset(candidate: list[object], parent: list[object]) -> bool:
    return set(candidate).issubset(set(parent))


def _safety_widens(candidate: dict[str, object] | None, parent: dict[str, object] | None) -> bool:
    candidate = candidate or {}
    parent = parent or {}
    for key in ("workspace_writes", "external_calls"):
        if bool(candidate.get(key, False)) and not bool(parent.get(key, False)):
            return True
    return _dict_widens(candidate, parent, ignore={"workspace_writes", "external_calls"})


def _budget_widens(candidate: dict[str, object] | None, parent: dict[str, object] | None) -> bool:
    candidate = candidate or {}
    parent = parent or {}
    for key, value in candidate.items():
        parent_value = parent.get(key)
        if _is_number(value) and _is_number(parent_value):
            if float(value) > float(parent_value):
                return True
        elif key not in parent and value not in (None, False, [], {}, 0):
            return True
        elif value != parent_value and key.lower().endswith(("cap", "limit", "max", "budget")):
            return True
    return False


def _dict_widens(candidate: dict[str, object] | None, parent: dict[str, object] | None, *, ignore: set[str] | None = None) -> bool:
    candidate = candidate or {}
    parent = parent or {}
    ignored = ignore or set()
    for key, value in candidate.items():
        if key in ignored:
            continue
        if key not in parent and value not in (None, False, [], {}, 0):
            return True
        parent_value = parent.get(key)
        if isinstance(value, bool) and value and not bool(parent_value):
            return True
    return False


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(char in string.hexdigits for char in value)
