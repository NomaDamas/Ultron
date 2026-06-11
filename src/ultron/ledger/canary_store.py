"""Canary-scoped stores and rollback no-poisoning checks."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ultron.ledger.side_effect_ledger import SideEffectLedger
from ultron.registry.pointer import ActivePointerStore

CANARY_NAMESPACES = ("memory", "skills", "ui_cache", "adapter_state", "pointer")


class RollbackReport(BaseModel):
    canary_id: str
    quarantined_entry_ids: list[str]
    dropped_namespaces: list[str]
    pointer_reverted: bool


class CanaryScopedStore:
    """Per-canary isolated stores plus separate baseline stores."""

    def __init__(self) -> None:
        self._canary: dict[str, dict[str, dict[str, Any]]] = {}
        self._baseline: dict[str, dict[str, Any]] = {namespace: {} for namespace in CANARY_NAMESPACES}

    def write(self, canary_id: str, namespace: str, key: str, value: Any) -> None:
        _validate_namespace(namespace)
        self._canary.setdefault(canary_id, {}).setdefault(namespace, {})[key] = value

    def read(self, canary_id: str, namespace: str, key: str) -> Any:
        _validate_namespace(namespace)
        return self._canary.get(canary_id, {}).get(namespace, {}).get(key)

    def read_namespace(self, canary_id: str, namespace: str) -> dict[str, Any]:
        _validate_namespace(namespace)
        return dict(self._canary.get(canary_id, {}).get(namespace, {}))

    def drop_canary(self, canary_id: str) -> list[str]:
        stores = self._canary.pop(canary_id, {})
        return [namespace for namespace in CANARY_NAMESPACES if namespace in stores]

    def baseline_write(self, namespace: str, key: str, value: Any) -> None:
        _validate_namespace(namespace)
        self._baseline[namespace][key] = value

    def baseline_read(self, namespace: str, key: str) -> Any:
        _validate_namespace(namespace)
        return self._baseline[namespace].get(key)


class RollbackController:
    def __init__(
        self,
        registry: Any | None = None,
        ledger: SideEffectLedger | None = None,
        canary_store: CanaryScopedStore | None = None,
        pointer_store: ActivePointerStore | None = None,
    ) -> None:
        self.registry = registry
        self.ledger = ledger or SideEffectLedger()
        self.canary_store = canary_store or CanaryScopedStore()
        self.pointer_store = pointer_store or ActivePointerStore()
        self._tracked_pointers: dict[str, _PointerRollbackState] = {}

    def track_pointer_candidate(
        self,
        canary_id: str,
        key: tuple[str, str],
        prior_version: int,
        prior_hashes: list[str],
        candidate_hashes: list[str],
    ) -> None:
        self._tracked_pointers[canary_id] = _PointerRollbackState(
            key=key,
            prior_version=prior_version,
            prior_hashes=list(prior_hashes),
            candidate_hashes=list(candidate_hashes),
        )

    def rollback(self, canary_id: str) -> RollbackReport:
        quarantined_entry_ids = self.ledger.mark_quarantined(canary_id)
        dropped_namespaces = self.canary_store.drop_canary(canary_id)
        pointer_reverted = self._revert_pointer(canary_id)
        return RollbackReport(
            canary_id=canary_id,
            quarantined_entry_ids=quarantined_entry_ids,
            dropped_namespaces=dropped_namespaces,
            pointer_reverted=pointer_reverted,
        )

    def assert_no_poisoning(self, canary_id: str) -> None:
        for namespace in CANARY_NAMESPACES:
            if self.canary_store.read_namespace(canary_id, namespace):
                raise AssertionError(f"canary state still readable in namespace {namespace}")
        if any(entry.canary_id == canary_id for entry in self.ledger.promotable_entries()):
            raise AssertionError("quarantined canary entry is promotable")
        state = self._tracked_pointers.get(canary_id)
        if state is not None:
            _, hashes = self.pointer_store.get(state.key)
            if hashes == state.candidate_hashes:
                raise AssertionError("active pointer still references canary candidate")

    def baseline_read(self, namespace: str, key: str) -> Any:
        return self.canary_store.baseline_read(namespace, key)

    def baseline_write(self, namespace: str, key: str, value: Any) -> None:
        self.canary_store.baseline_write(namespace, key, value)

    def _revert_pointer(self, canary_id: str) -> bool:
        state = self._tracked_pointers.get(canary_id)
        if state is None:
            return False
        current_version, current_hashes = self.pointer_store.get(state.key)
        if current_hashes != state.candidate_hashes:
            return False
        try:
            self.pointer_store.swap(state.key, current_version, state.prior_hashes)
        except ValueError:
            return False
        return True


class _PointerRollbackState(BaseModel):
    key: tuple[str, str]
    prior_version: int
    prior_hashes: list[str] = Field(default_factory=list)
    candidate_hashes: list[str] = Field(default_factory=list)


def _validate_namespace(namespace: str) -> None:
    if namespace not in CANARY_NAMESPACES:
        raise ValueError(f"unknown canary namespace: {namespace}")
