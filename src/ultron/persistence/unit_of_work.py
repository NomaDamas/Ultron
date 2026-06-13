"""Atomic promotion/prune/restore unit-of-work helpers."""

from __future__ import annotations

from typing import Any

from ultron.ledger.side_effect_ledger import LedgerEntry, SideEffectKind
from ultron.persistence.db import Database
from ultron.persistence.sqlite_stores import SqliteActivePointerStore, SqliteModuleRegistry, SqliteSideEffectLedger
from ultron.registry.store import ModuleLifecycle


class PromotionUnitOfWork:
    def __init__(self, db: Database, registry: SqliteModuleRegistry, pointer_store: SqliteActivePointerStore, ledger: SqliteSideEffectLedger) -> None:
        self.db = db
        self.registry = registry
        self.pointer_store = pointer_store
        self.ledger = ledger

    def promote(self, candidate_hash: str, expected_pointer_version: int, new_hashes: list[str], evidence_id: str, actor: str, *, key: tuple[str, str] = ("default-user", "code-triage")) -> int:
        return self._transition(candidate_hash, ModuleLifecycle.SURVIVOR, expected_pointer_version, new_hashes, evidence_id, actor, "promote", key)

    def prune(self, module_hash: str, expected_pointer_version: int, new_hashes: list[str], evidence_id: str, actor: str, *, key: tuple[str, str] = ("default-user", "code-triage")) -> int:
        return self._transition(module_hash, ModuleLifecycle.PRUNED, expected_pointer_version, new_hashes, evidence_id, actor, "prune", key)

    def restore(self, module_hash: str, expected_pointer_version: int, new_hashes: list[str], evidence_id: str, actor: str, *, key: tuple[str, str] = ("default-user", "code-triage"), pruned_hashes: list[str] | None = None) -> int:
        return self._transition(module_hash, ModuleLifecycle.SURVIVOR, expected_pointer_version, new_hashes, evidence_id, actor, "restore", key, pruned_hashes=pruned_hashes)


    def _transition(self, module_hash: str, lifecycle: ModuleLifecycle, expected_pointer_version: int, new_hashes: list[str], evidence_id: str, actor: str, action: str, key: tuple[str, str], *, pruned_hashes: list[str] | None = None) -> int:
        with self.db.tx() as cur:
            prior_version, prior_hashes = self.pointer_store.get(key)
            cur.execute("UPDATE module_lifecycle SET lifecycle = ? WHERE content_hash = ?", (lifecycle.value, module_hash))
            if cur.rowcount != 1:
                raise KeyError(module_hash)
            for pruned_hash in pruned_hashes or []:
                result = cur.execute("UPDATE module_lifecycle SET lifecycle = ? WHERE content_hash = ?", (ModuleLifecycle.PRUNED.value, pruned_hash))
                if result.rowcount != 1:
                    raise KeyError(pruned_hash)
            new_version = self.pointer_store._swap_in_tx(cur, key, expected_pointer_version, new_hashes)
            entry = LedgerEntry(
                run_id=evidence_id,
                module_set_hash=module_hash,
                module_hash=module_hash,
                kind=SideEffectKind.POINTER_TRANSITION,
                payload={
                    "action": action,
                    "prior_version": prior_version,
                    "new_version": new_version,
                    "prior_hashes": prior_hashes,
                    "new_hashes": list(new_hashes),
                    "evidence_id": evidence_id,
                    "actor": actor,
                    "scope_key": list(key),
                },
            )
            self.ledger._append_in_tx(cur, entry)
            return new_version
