"""Deterministic module composition resolver."""

from __future__ import annotations

from typing import Any, Iterable

from ultron.composition.manifest import ModuleSetManifest, SurfaceConflict
from ultron.hermes.capability import AdapterCapabilityContract, CapabilityStatus
from ultron.hermes.module_surface_contract import MODULE_SURFACE_MAP
from ultron.registry.store import ModuleRegistry, RegistryEntry


LAYER_RANK = {"global": 1, "tenant": 2, "user": 3, "canary": 4}


class CompositionResolver:
    def __init__(self, registry: ModuleRegistry, adapter_contract: AdapterCapabilityContract) -> None:
        self.registry = registry
        self.adapter_contract = adapter_contract

    def resolve(
        self,
        user_scope: str,
        workflow_fingerprint: str,
        request_class: str,
        active_module_hashes: Iterable[str],
        ui_registry: set[str],
        safety_floor: dict[str, Any] | None = None,
    ) -> ModuleSetManifest:
        conflicts: list[SurfaceConflict] = []
        disabled_modules: list[str] = []
        enabled: list[RegistryEntry] = []

        for content_hash in sorted(set(active_module_hashes)):
            entry = self.registry.get(content_hash)
            deferred = _deferred_reasons(entry, self.adapter_contract)
            if deferred:
                disabled_modules.append(content_hash)
                conflicts.append(
                    SurfaceConflict(
                        surface="adapter_contract",
                        kind="deferred_surface",
                        winner_hash=None,
                        losers=[content_hash],
                        rationale="; ".join(sorted(deferred)),
                    )
                )
            else:
                enabled.append(entry)

        enabled = sorted(enabled, key=_entry_sort_key)
        ordered_hashes = [entry.module.content_hash or "" for entry in enabled]
        resolved_tools, tool_conflicts = _resolve_tools(enabled)
        conflicts.extend(tool_conflicts)
        safety_policy, safety_conflicts = _merge_restrictive("safety", enabled, safety_floor or {})
        budget_policy, budget_conflicts = _merge_restrictive("budget", enabled, {})
        conflicts.extend(safety_conflicts)
        conflicts.extend(budget_conflicts)
        prompt_order = _resolve_prompts(enabled)
        skill_refs = _resolve_skill_refs(enabled)
        ui_panels, ui_disabled, ui_conflicts = _resolve_ui_panels(enabled, ui_registry)
        disabled_modules.extend(ui_disabled)
        conflicts.extend(ui_conflicts)

        conflicts = sorted(
            conflicts,
            key=lambda conflict: (
                conflict.surface,
                conflict.kind,
                conflict.winner_hash or "",
                tuple(conflict.losers),
                conflict.rationale,
            ),
        )
        disabled_modules = sorted(set(disabled_modules))

        manifest = ModuleSetManifest(
            user_scope=user_scope,
            workflow_fingerprint=workflow_fingerprint,
            request_class=request_class,
            ordered_module_hashes=ordered_hashes,
            resolved_prompt_order=prompt_order,
            resolved_tool_allowlist=resolved_tools,
            resolved_skill_refs=skill_refs,
            resolved_ui_panels=ui_panels,
            disabled_modules=disabled_modules,
            conflicts=conflicts,
            safety_policy=safety_policy,
            budget_policy=budget_policy,
            rationale="deterministic resolver: sorted inputs, layer/module/hash ordering, restrictive safety/budget, tool intersection by default",
        )
        return manifest.finalized()


def _entry_sort_key(entry: RegistryEntry) -> tuple[int, str, str]:
    return (LAYER_RANK[entry.layer], entry.module.module_id, entry.module.content_hash or "")


def _deferred_reasons(entry: RegistryEntry, contract: AdapterCapabilityContract) -> list[str]:
    module = entry.module
    reasons: list[str] = []
    declared = module.surfaces.model_dump()
    for surface_name, attach_surface in MODULE_SURFACE_MAP.items():
        value = declared.get(surface_name)
        if value in (None, [], {}, False):
            continue
        if contract.get(attach_surface).status == CapabilityStatus.DEFERRED:
            reasons.append(f"{surface_name} deferred: {attach_surface.value}")
    for capability in module.required_adapter_capabilities:
        if contract.get(capability).status == CapabilityStatus.DEFERRED:
            reasons.append(f"required capability deferred: {capability.value}")
    return reasons


def _resolve_tools(enabled: list[RegistryEntry]) -> tuple[list[str], list[SurfaceConflict]]:
    if not enabled:
        return [], []
    tool_sets = [set(entry.module.surfaces.tools) for entry in enabled]
    intersection = set.intersection(*tool_sets) if tool_sets else set()
    union = set().union(*tool_sets)
    additive_tools = set().union(
        *(set(entry.module.surfaces.tools) for entry in enabled if entry.human_approved_additive)
    )
    resolved = intersection | additive_tools
    dropped = sorted(union - resolved)
    conflicts: list[SurfaceConflict] = []
    if dropped:
        conflicts.append(
            SurfaceConflict(
                surface="tools",
                kind="intersection_dropped_tools",
                winner_hash=None,
                losers=sorted(entry.module.content_hash or "" for entry in enabled),
                rationale="tool allowlist uses intersection by default; dropped: " + ", ".join(dropped),
            )
        )
    added = sorted(additive_tools - intersection)
    if added:
        conflicts.append(
            SurfaceConflict(
                surface="tools",
                kind="approved_additive_tools",
                winner_hash=None,
                losers=sorted(
                    entry.module.content_hash or "" for entry in enabled if entry.human_approved_additive
                ),
                rationale="human-approved additive tools added: " + ", ".join(added),
            )
        )
    return sorted(resolved), conflicts


def _resolve_prompts(enabled: list[RegistryEntry]) -> list[str]:
    ordered: list[str] = []
    for entry in sorted(enabled, key=lambda item: (LAYER_RANK[item.layer], item.module.module_id, item.module.content_hash or "")):
        ordered.extend(entry.module.surfaces.prompt_slots)
    return ordered


def _resolve_skill_refs(enabled: list[RegistryEntry]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for entry in sorted(enabled, key=lambda item: (LAYER_RANK[item.layer], item.module.module_id, item.module.content_hash or "")):
        for skill_ref in entry.module.surfaces.skill_refs:
            if skill_ref not in seen:
                seen.add(skill_ref)
                ordered.append(skill_ref)
    return ordered


def _resolve_ui_panels(
    enabled: list[RegistryEntry], ui_registry: set[str]
) -> tuple[list[str], list[str], list[SurfaceConflict]]:
    kept: list[tuple[int, str, str, str]] = []
    disabled: list[str] = []
    conflicts: list[SurfaceConflict] = []
    for entry in enabled:
        missing = sorted(panel for panel in entry.module.surfaces.ui_panels if _panel_name(panel) not in ui_registry)
        if missing:
            disabled.append(entry.module.content_hash or "")
            conflicts.append(
                SurfaceConflict(
                    surface="ui_panels",
                    kind="panel_not_registered",
                    winner_hash=None,
                    losers=[entry.module.content_hash or ""],
                    rationale="dropped panels absent from ui_registry: " + ", ".join(missing),
                )
            )
        for panel in entry.module.surfaces.ui_panels:
            name = _panel_name(panel)
            if name in ui_registry:
                kept.append((_panel_priority(panel), entry.module.module_id, entry.module.content_hash or "", name))
    return [name for _, _, _, name in sorted(kept)], disabled, conflicts


def _panel_name(panel: str) -> str:
    return panel.split(":", 1)[0]


def _panel_priority(panel: str) -> int:
    if ":" not in panel:
        return 0
    _, priority = panel.split(":", 1)
    try:
        return int(priority)
    except ValueError:
        return 0


def _merge_restrictive(
    surface: str,
    enabled: list[RegistryEntry],
    floor: dict[str, Any],
) -> tuple[dict[str, Any], list[SurfaceConflict]]:
    policies: list[tuple[str, dict[str, Any]]] = []
    if floor:
        policies.append(("floor", floor))
    for entry in enabled:
        policy = getattr(entry.module.surfaces, surface)
        if policy:
            policies.append((entry.module.content_hash or "", policy))
    if not policies:
        return {}, []

    keys = sorted({key for _, policy in policies for key in policy})
    resolved: dict[str, Any] = {}
    conflicts: list[SurfaceConflict] = []
    for key in keys:
        values = [(owner, policy[key]) for owner, policy in policies if key in policy]
        winner_owner, winner_value = _most_restrictive_value(values)
        resolved[key] = winner_value
        if len({repr(value) for _, value in values}) > 1:
            conflicts.append(
                SurfaceConflict(
                    surface=f"{surface}.{key}",
                    kind="most_restrictive_wins",
                    winner_hash=None if winner_owner == "floor" else winner_owner,
                    losers=sorted(owner for owner, value in values if owner != winner_owner and owner != "floor"),
                    rationale="numeric caps use minimum; boolean allow flags use deny-overrides-allow",
                )
            )
    return dict(sorted(resolved.items())), conflicts


def _most_restrictive_value(values: list[tuple[str, Any]]) -> tuple[str, Any]:
    if all(isinstance(value, bool) for _, value in values):
        for owner, value in sorted(values, key=lambda item: (item[1], item[0])):
            if value is False:
                return owner, False
        return sorted(values, key=lambda item: item[0])[0][0], True
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for _, value in values):
        return min(values, key=lambda item: (item[1], item[0]))
    return sorted(values, key=lambda item: (repr(item[1]), item[0]))[0]
