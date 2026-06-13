"""Atomic promotion/prune/restore unit-of-work helpers."""

from __future__ import annotations

from typing import Any

from ultron.evolution.loop import plan_active_set_transition

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

    def promote(self, candidate_hash: str, expected_pointer_version: int, new_hashes: list[str], evidence_id: str, actor: str, *, key: tuple[str, str] = ("default-user", "code-triage"), active_module_cap: int | None = None) -> int:
        return self._transition(candidate_hash, ModuleLifecycle.SURVIVOR, expected_pointer_version, new_hashes, evidence_id, actor, "promote", key, active_module_cap=active_module_cap)

    def prune(
        self,
        module_hash: str,
        expected_pointer_version: int,
        new_hashes: list[str],
        evidence_id: str,
        actor: str,
        *,
        key: tuple[str, str] = ("default-user", "code-triage"),
        is_critical_seed: bool = False,
        approved: bool = False,
        diversity_floor: int = 0,
        current_active_hashes: list[str] | None = None,
    ) -> int:
        return self._transition(
            module_hash,
            ModuleLifecycle.PRUNED,
            expected_pointer_version,
            new_hashes,
            evidence_id,
            actor,
            "prune",
            key,
            is_critical_seed=is_critical_seed,
            approved=approved,
            diversity_floor=diversity_floor,
            current_active_hashes=current_active_hashes,
        )

    def restore(self, module_hash: str, expected_pointer_version: int, new_hashes: list[str], evidence_id: str, actor: str, *, key: tuple[str, str] = ("default-user", "code-triage"), pruned_hashes: list[str] | None = None) -> int:
        return self._transition(module_hash, ModuleLifecycle.SURVIVOR, expected_pointer_version, new_hashes, evidence_id, actor, "restore", key, pruned_hashes=pruned_hashes)


    def _transition(self, module_hash: str, lifecycle: ModuleLifecycle, expected_pointer_version: int, new_hashes: list[str], evidence_id: str, actor: str, action: str, key: tuple[str, str], *, pruned_hashes: list[str] | None = None, active_module_cap: int | None = None, is_critical_seed: bool = False, approved: bool = False, diversity_floor: int = 0, current_active_hashes: list[str] | None = None) -> int:
        with self.db.tx() as cur:
            prior_version, prior_hashes = self.pointer_store.get(key)
            transition_pruned_hashes = list(pruned_hashes or [])
            if action == "promote" and active_module_cap is not None:
                plan = plan_active_set_transition(self.registry, module_hash, prior_hashes, active_module_cap)
                new_hashes = plan.new_active
                transition_pruned_hashes.extend(plan.evicted)
            if action == "prune":
                if current_active_hashes is not None and list(current_active_hashes) != list(prior_hashes):
                    raise ValueError("stale active pointer state")
                if is_critical_seed and not approved:
                    raise ValueError("critical seed pruning requires approval")
                if len(new_hashes) < diversity_floor:
                    raise ValueError("prune would breach diversity floor")
            cur.execute("UPDATE module_lifecycle SET lifecycle = ? WHERE content_hash = ?", (lifecycle.value, module_hash))
            if cur.rowcount != 1:
                raise KeyError(module_hash)
            for pruned_hash in transition_pruned_hashes:
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
                    "evicted_hashes": list(transition_pruned_hashes),
                    "evidence_id": evidence_id,
                    "actor": actor,
                    "scope_key": list(key),
                },
            )
            self.ledger._append_in_tx(cur, entry)
            return new_version
