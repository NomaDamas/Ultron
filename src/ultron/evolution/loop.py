"""Promotion, active-set stability, and reversible atrophy controls."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel

from ultron.evolution.selection import SelectionOutcome, Selector
from ultron.registry.pointer import ActivePointerStore
from ultron.registry.store import ModuleLifecycle, ModuleRegistry


class StabilityControls(BaseModel):
    variant_budget: int = 3
    active_module_cap: int = 8
    diversity_floor: int = 2
    promotion_cooldown_s: float = 0
    prune_cooldown_s: float = 0


class EvolutionLoop:
    def __init__(
        self,
        registry: ModuleRegistry,
        pointer_store: ActivePointerStore,
        selector: Selector,
        controls: StabilityControls,
    ) -> None:
        self.registry = registry
        self.pointer_store = pointer_store
        self.selector = selector
        self.controls = controls
        self._last_promotion_at: dict[str, float] = {}
        self._last_prune_at: dict[str, float] = {}
        self._rollback_counts: dict[str, int] = {}
        self._conflicts: set[str] = set()
        self._critical_seeds: set[str] = set()

    def register_candidate(self, candidate_hash: str, *args: Any, **kwargs: Any) -> bool:
        entry = self.registry.get(candidate_hash)
        parent_hash = entry.module.parent_id
        if parent_hash is None:
            raise ValueError("candidate must have parent_id")
        candidate_count = sum(
            1
            for version in self.registry.versions_of(entry.module.module_id)
            if version.module.parent_id == parent_hash and version.lifecycle == ModuleLifecycle.CANDIDATE
        )
        if candidate_count > self.controls.variant_budget:
            self.registry.set_lifecycle(candidate_hash, ModuleLifecycle.DECAYING)
            raise ValueError("variant budget exceeded for parent")
        return True

    def retain(
        self,
        candidate_hash: str,
        outcome: SelectionOutcome,
        user_scope: str,
        workflow_fingerprint: str,
        expected_version: int,
    ) -> bool:
        if not outcome.promotable:
            self.registry.set_lifecycle(candidate_hash, ModuleLifecycle.DECAYING)
            return False
        key = (user_scope, workflow_fingerprint)
        self._check_promotion_cooldown(key)
        version, active = self.pointer_store.get(key)
        if version != expected_version:
            raise ValueError("stale active pointer version")
        new_active = list(active)
        if candidate_hash not in new_active:
            new_active.append(candidate_hash)
        evicted: list[str] = []
        while len(new_active) > self.controls.active_module_cap:
            evict = self._eviction_candidate(new_active, protected={candidate_hash})
            if evict is None:
                raise ValueError("active module cap cannot be satisfied without evicting protected candidate")
            new_active.remove(evict)
            evicted.append(evict)
        self.pointer_store.swap(key, expected_version, new_active)
        self.registry.set_lifecycle(candidate_hash, ModuleLifecycle.SURVIVOR)
        for module_hash in evicted:
            self.registry.set_lifecycle(module_hash, ModuleLifecycle.PRUNED)
            self._last_prune_at[module_hash] = time.time()
        self._last_promotion_at[self._cooldown_key(key)] = time.time()
        return True

    def atrophy_scan(self, active_hashes: list[str], now: float) -> list[str]:
        eligible: list[str] = []
        distinct_floor = max(self.controls.diversity_floor, 0)
        remaining = len(active_hashes)
        for module_hash in active_hashes:
            if remaining <= distinct_floor:
                break
            if module_hash in self._critical_seeds:
                continue
            if self._is_prune_cooling_down(module_hash, now):
                continue
            entry = self.registry.get(module_hash)
            fitness = entry.module.fitness
            stale = fitness.usage_count <= 0 and fitness.last_used_at is not None and fitness.last_used_at < now
            negative = (fitness.primary_metric is not None and fitness.primary_metric < 0) or fitness.decay_score >= 1.0
            repeated_rollback = self._rollback_counts.get(module_hash, 0) >= 2
            conflict = module_hash in self._conflicts
            if stale or negative or repeated_rollback or conflict:
                eligible.append(module_hash)
                remaining -= 1
        return eligible

    def prune(self, module_hash: str, *, is_critical_seed: bool = False, approved: bool = False) -> bool:
        if is_critical_seed and not approved:
            raise ValueError("critical seed pruning requires approval")
        now = time.time()
        if self._is_prune_cooling_down(module_hash, now):
            raise ValueError("prune cooldown active")
        changed = False
        for key in self._active_keys_containing(module_hash):
            version, active = self.pointer_store.get(key)
            if module_hash not in active:
                continue
            if len(active) - 1 < self.controls.diversity_floor:
                raise ValueError("prune would breach diversity floor")
            self.pointer_store.swap(key, version, [h for h in active if h != module_hash])
            changed = True
        self.registry.set_lifecycle(module_hash, ModuleLifecycle.PRUNED)
        self._last_prune_at[module_hash] = now
        return changed

    def restore(
        self,
        module_hash: str,
        user_scope: str,
        workflow_fingerprint: str,
        expected_version: int,
    ) -> bool:
        key = (user_scope, workflow_fingerprint)
        version, active = self.pointer_store.get(key)
        if version != expected_version:
            raise ValueError("stale active pointer version")
        if module_hash in active:
            self.registry.set_lifecycle(module_hash, ModuleLifecycle.SURVIVOR)
            return False
        new_active = list(active) + [module_hash]
        while len(new_active) > self.controls.active_module_cap:
            evict = self._eviction_candidate(new_active, protected={module_hash})
            if evict is None:
                raise ValueError("active module cap cannot be satisfied")
            new_active.remove(evict)
            self.registry.set_lifecycle(evict, ModuleLifecycle.PRUNED)
        self.pointer_store.swap(key, expected_version, new_active)
        self.registry.set_lifecycle(module_hash, ModuleLifecycle.SURVIVOR)
        return True

    def mark_rollback(self, module_hash: str) -> None:
        self._rollback_counts[module_hash] = self._rollback_counts.get(module_hash, 0) + 1

    def mark_conflict(self, module_hash: str) -> None:
        self._conflicts.add(module_hash)

    def mark_critical_seed(self, module_hash: str) -> None:
        self._critical_seeds.add(module_hash)

    def _check_promotion_cooldown(self, key: tuple[str, str]) -> None:
        if self.controls.promotion_cooldown_s <= 0:
            return
        last = self._last_promotion_at.get(self._cooldown_key(key))
        if last is not None and time.time() - last < self.controls.promotion_cooldown_s:
            raise ValueError("promotion cooldown active")

    def _is_prune_cooling_down(self, module_hash: str, now: float) -> bool:
        if self.controls.prune_cooldown_s <= 0:
            return False
        last = self._last_prune_at.get(module_hash)
        return last is not None and now - last < self.controls.prune_cooldown_s

    def _eviction_candidate(self, active_hashes: list[str], protected: set[str]) -> str | None:
        candidates = [h for h in active_hashes if h not in protected]
        if not candidates:
            return None
        def score(module_hash: str) -> tuple[float, float]:
            entry = self.registry.get(module_hash)
            primary = entry.module.fitness.primary_metric
            metric = primary if primary is not None else float("-inf")
            return (metric - entry.module.fitness.decay_score, entry.created_at)
        return min(candidates, key=score)

    def _active_keys_containing(self, module_hash: str) -> list[tuple[str, str]]:
        pointers = getattr(self.pointer_store, "_pointers", {})
        return [key for key, (_, hashes) in pointers.items() if module_hash in hashes]

    def _cooldown_key(self, key: tuple[str, str]) -> str:
        return "\0".join(key)
