"""Canary-scoped stores and rollback no-poisoning checks."""

from __future__ import annotations
from copy import deepcopy

from typing import Any

from pydantic import BaseModel, Field

from ultron.auth.principal import DEFAULT_LOCAL_PRINCIPAL
from ultron.ledger.side_effect_ledger import LedgerEntry, SideEffectKind, SideEffectLedger
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
        self._canary.setdefault(canary_id, {}).setdefault(namespace, {})[key] = deepcopy(value)

    def read(self, canary_id: str, namespace: str, key: str) -> Any:
        _validate_namespace(namespace)
        return deepcopy(self._canary.get(canary_id, {}).get(namespace, {}).get(key))

    def read_namespace(self, canary_id: str, namespace: str) -> dict[str, Any]:
        _validate_namespace(namespace)
        return deepcopy(self._canary.get(canary_id, {}).get(namespace, {}))

    def drop_canary(self, canary_id: str) -> list[str]:
        stores = self._canary.pop(canary_id, {})
        return [namespace for namespace in CANARY_NAMESPACES if namespace in stores]

    def baseline_write(self, namespace: str, key: str, value: Any) -> None:
        _validate_namespace(namespace)
        self._baseline[namespace][key] = deepcopy(value)

    def baseline_read(self, namespace: str, key: str) -> Any:
        _validate_namespace(namespace)
        return deepcopy(self._baseline[namespace].get(key))


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
        candidate_version: int | None = None,
        run_id: str = "pointer-transition",
        module_set_hash: str = "pointer-transition",
        actor: str | None = None,
    ) -> None:
        audit_actor = actor or DEFAULT_LOCAL_PRINCIPAL.subject
        state = _PointerRollbackState(
            key=key,
            prior_version=prior_version,
            prior_hashes=list(prior_hashes),
            candidate_version=candidate_version if candidate_version is not None else prior_version + 1,
            candidate_hashes=list(candidate_hashes),
        )
        self._tracked_pointers[canary_id] = state
        self.ledger.append(
            LedgerEntry(
                run_id=run_id,
                module_set_hash=module_set_hash,
                canary_id=canary_id,
                kind=SideEffectKind.POINTER_TRANSITION,
                payload={**state.to_ledger_payload(), "actor": audit_actor},
                actor=audit_actor,
            )
        )

    def rollback(self, canary_id: str, actor: str | None = None) -> RollbackReport:
        if not actor:
            raise ValueError("rollback actor is required")
        quarantined_entry_ids = self.ledger.mark_quarantined(canary_id, actor=actor)
        dropped_namespaces = self.canary_store.drop_canary(canary_id)
        pointer_reverted = self._revert_pointer(canary_id)
        report = RollbackReport(
            canary_id=canary_id,
            quarantined_entry_ids=quarantined_entry_ids,
            dropped_namespaces=dropped_namespaces,
            pointer_reverted=pointer_reverted,
        )
        self.assert_no_poisoning(canary_id)
        return report

    def assert_no_poisoning(self, canary_id: str) -> None:
        for namespace in CANARY_NAMESPACES:
            if self.canary_store.read_namespace(canary_id, namespace):
                raise AssertionError(f"canary state still readable in namespace {namespace}")
        if any(entry.canary_id == canary_id for entry in self.ledger.promotable_entries()):
            raise AssertionError("quarantined canary entry is promotable")
        for state in self._pointer_transitions(canary_id):
            version, hashes = self.pointer_store.get(state.key)
            if version == state.candidate_version and hashes == state.candidate_hashes:
                raise AssertionError("active pointer still references canary candidate")

    def baseline_read(self, namespace: str, key: str) -> Any:
        return self.canary_store.baseline_read(namespace, key)

    def baseline_write(self, namespace: str, key: str, value: Any) -> None:
        self.canary_store.baseline_write(namespace, key, value)

    def _revert_pointer(self, canary_id: str) -> bool:
        pointer_reverted = False
        for state in reversed(self._pointer_transitions(canary_id)):
            if state.prior_version is None:
                raise RuntimeError("cannot rollback pointer transition without prior version")
            current_version, current_hashes = self.pointer_store.get(state.key)
            if current_hashes != state.candidate_hashes:
                continue
            try:
                self.pointer_store.swap(state.key, current_version, state.prior_hashes)
            except ValueError as exc:
                raise RuntimeError("failed to rollback active pointer transition") from exc
            pointer_reverted = True
        return pointer_reverted

    def _pointer_transitions(self, canary_id: str) -> list["_PointerRollbackState"]:
        states: list[_PointerRollbackState] = []
        for entry in self.ledger.entries_for_canary(canary_id):
            if entry.kind != SideEffectKind.POINTER_TRANSITION:
                continue
            try:
                states.append(_PointerRollbackState.from_ledger_payload(entry.payload))
            except RuntimeError:
                if canary_id in self._tracked_pointers:
                    continue
                raise
        if canary_id in self._tracked_pointers:
            tracked = self._tracked_pointers[canary_id]
            if tracked not in states:
                states.append(tracked)
        return states


class _PointerRollbackState(BaseModel):
    key: tuple[str, str]
    prior_version: int | None
    prior_hashes: list[str] = Field(default_factory=list)
    candidate_version: int
    candidate_hashes: list[str] = Field(default_factory=list)

    def to_ledger_payload(self) -> dict[str, Any]:
        return {
            "key": list(self.key),
            "prior_version": self.prior_version,
            "prior_hashes": list(self.prior_hashes),
            "candidate_version": self.candidate_version,
            "candidate_hashes": list(self.candidate_hashes),
        }

    @classmethod
    def from_ledger_payload(cls, payload: dict[str, Any]) -> "_PointerRollbackState":
        try:
            key = payload["key"]
            candidate_version = payload["candidate_version"]
        except KeyError as exc:
            raise RuntimeError("pointer transition ledger entry is missing rollback data") from exc
        return cls(
            key=(key[0], key[1]),
            prior_version=payload.get("prior_version"),
            prior_hashes=list(payload.get("prior_hashes", [])),
            candidate_version=candidate_version,
            candidate_hashes=list(payload.get("candidate_hashes", [])),
        )


def _validate_namespace(namespace: str) -> None:
    if namespace not in CANARY_NAMESPACES:
        raise ValueError(f"unknown canary namespace: {namespace}")
