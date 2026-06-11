"""Bounded one-primitive canary variation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ultron.hermes.capability import AttachSurface, CapabilityStatus
from ultron.module.model import FitnessMetadata, HarnessModule, PromotionState
from ultron.registry.store import ModuleLifecycle, ModuleRegistry


class VariationPrimitive(StrEnum):
    PROMPT_SLOT_EDIT = "PROMPT_SLOT_EDIT"
    TOOLSET_TOGGLE = "TOOLSET_TOGGLE"
    UI_PANEL_PRIORITY = "UI_PANEL_PRIORITY"
    PLANNING_DEPTH = "PLANNING_DEPTH"
    BUDGET_TIGHTEN = "BUDGET_TIGHTEN"
    SAFETY_TIGHTEN = "SAFETY_TIGHTEN"
    TOPOLOGY_CHANGE = "TOPOLOGY_CHANGE"


class MutationProposal(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    parent_hash: str
    primitive: VariationPrimitive
    change: dict[str, Any] = Field(default_factory=dict)
    rationale: str
    requires_human_approval: bool = False
    human_approved: bool = False


_PRIMITIVE_FIELDS: dict[VariationPrimitive, frozenset[str]] = {
    VariationPrimitive.PROMPT_SLOT_EDIT: frozenset({"prompt_pack_hash", "surfaces.prompt_slots", "prompt_slots"}),
    VariationPrimitive.TOOLSET_TOGGLE: frozenset({"tool_allowlist_hash", "surfaces.tools", "tools"}),
    VariationPrimitive.UI_PANEL_PRIORITY: frozenset({"ui_panel_contract_hash", "surfaces.ui_panels", "ui_panels"}),
    VariationPrimitive.PLANNING_DEPTH: frozenset({"skill_refs", "workflow_tags"}),
    VariationPrimitive.BUDGET_TIGHTEN: frozenset({"budget_policy_hash", "surfaces.budget", "budget"}),
    VariationPrimitive.SAFETY_TIGHTEN: frozenset({"safety_policy_hash", "surfaces.safety", "safety"}),
    VariationPrimitive.TOPOLOGY_CHANGE: frozenset({"topology_fragment_hash", "surfaces.topology_fragment", "topology_fragment"}),
}

_SIDE_EFFECT_FIELDS = frozenset({
    "persistence_policy",
    "surfaces.persistence",
    "persistence",
    "required_adapter_capabilities",
})
_CORE_CUSTOMIZATION_FIELDS = frozenset({
    "module_id",
    "hermes_version_range",
    "owner_scope",
    "privacy",
    "target_lens",
})


class VariationEngine:
    def __init__(self, registry: ModuleRegistry, adapter_contract: Any) -> None:
        self.registry = registry
        self.adapter_contract = adapter_contract

    def propose(
        self,
        parent_hash: str,
        primitive: VariationPrimitive,
        change: dict[str, Any],
        human_approved: bool = False,
    ) -> MutationProposal:
        primitive = VariationPrimitive(primitive)
        if not change:
            raise ValueError("variation change must contain one edit")
        touched = set(change)
        allowed = _PRIMITIVE_FIELDS[primitive]
        unauthorized = touched - allowed
        approval_only = unauthorized & (_SIDE_EFFECT_FIELDS | _CORE_CUSTOMIZATION_FIELDS)
        compound = len(touched) != 1 or bool(unauthorized - approval_only)
        if compound and not human_approved:
            raise ValueError("compound variation requires human approval")
        self._reject_topology_change(primitive, change)

        parent = self.registry.get(parent_hash).module
        candidate = self._candidate_from_change(parent, primitive, change)
        requires_human_approval = self._requires_human_approval(parent, candidate, primitive, change)
        rationale = f"Apply {primitive.value} to {', '.join(sorted(touched))}"
        return MutationProposal(
            parent_hash=parent_hash,
            primitive=primitive,
            change=dict(change),
            rationale=rationale,
            requires_human_approval=requires_human_approval,
            human_approved=human_approved,
        )

    def _validate_proposal(self, proposal: MutationProposal) -> HarnessModule:
        parent = self.registry.get(proposal.parent_hash).module
        candidate = self._candidate_from_change(parent, proposal.primitive, proposal.change)
        self._reject_topology_change(proposal.primitive, proposal.change)
        requires_human_approval = self._requires_human_approval(
            parent,
            candidate,
            proposal.primitive,
            proposal.change,
        )
        if requires_human_approval and not proposal.human_approved:
            raise ValueError("mutation proposal requires human approval")
        return candidate

    def apply(self, proposal: MutationProposal) -> HarnessModule:
        proposal = MutationProposal.model_validate(proposal)
        candidate = self._validate_proposal(proposal)
        registered = self.registry.register(
            candidate,
            ModuleLifecycle.CANDIDATE,
            "canary",
            human_approved_additive=proposal.human_approved,
        )
        return registered.module

    def _candidate_from_change(
        self,
        parent: HarnessModule,
        primitive: VariationPrimitive,
        change: dict[str, Any],
    ) -> HarnessModule:
        data = parent.model_dump(mode="python", exclude={"content_hash"})
        data["parent_id"] = parent.content_hash
        data["version"] = parent.version + 1
        data["fitness"] = FitnessMetadata(promotion_state=PromotionState.CANDIDATE)
        for field, value in change.items():
            _set_candidate_field(data, field, value)
        candidate = HarnessModule.model_validate(data).finalized()
        candidate.validate_surfaces(self.adapter_contract)
        return candidate

    def _requires_human_approval(
        self,
        parent: HarnessModule,
        candidate: HarnessModule,
        primitive: VariationPrimitive,
        change: dict[str, Any],
    ) -> bool:
        touched = set(change)
        allowed = _PRIMITIVE_FIELDS[primitive]
        unauthorized = touched - allowed
        approval_only = unauthorized & (_SIDE_EFFECT_FIELDS | _CORE_CUSTOMIZATION_FIELDS)
        compound = len(touched) != 1 or bool(unauthorized - approval_only)
        return compound or bool(approval_only) or not self.registry.can_auto_promote(candidate)

    def _reject_topology_change(self, primitive: VariationPrimitive, change: dict[str, Any]) -> None:
        if {"topology_fragment_hash", "surfaces.topology_fragment", "topology_fragment"} & set(change):
            raise ValueError("topology/deferred surface changes are not auto-applied by variation primitives")
        if primitive is VariationPrimitive.TOPOLOGY_CHANGE:
            raise ValueError("topology/deferred surface changes are not auto-applied by variation primitives")
        for field, value in change.items():
            if field in {"required_adapter_capabilities", "surfaces", "surfaces.topology_fragment", "topology_fragment"}:
                candidate_data = value if field == "surfaces" else {field: value}
                if _declares_deferred_surface(candidate_data, self.adapter_contract):
                    raise ValueError("deferred attach surface rejected")



def _set_candidate_field(data: dict[str, Any], field: str, value: Any) -> None:
    if field.startswith("surfaces."):
        _, surface_field = field.split(".", 1)
        surfaces = dict(data["surfaces"])
        surfaces[surface_field] = value
        data["surfaces"] = surfaces
        return
    if field in {"prompt_slots", "tools", "ui_panels", "budget", "safety", "topology_fragment", "persistence"}:
        surfaces = dict(data["surfaces"])
        surfaces[field] = value
        data["surfaces"] = surfaces
        return
    data[field] = value


def _declared_surface_names(module: HarnessModule) -> set[str]:
    declared: set[str] = set()
    for name, value in module.surfaces.model_dump().items():
        if value not in (None, [], {}, False):
            declared.add(name)
    return declared


def _declares_deferred_surface(change: dict[str, Any], contract: Any) -> bool:
    surfaces = set()
    required = change.get("required_adapter_capabilities", []) or []
    for surface in required:
        if contract.get(AttachSurface(surface)).status == CapabilityStatus.DEFERRED:
            return True
    for field, value in change.items():
        if field.startswith("surfaces."):
            surface_name = field.split(".", 1)[1]
            if value not in (None, [], {}, False):
                surfaces.add(surface_name)
        elif field == "surfaces" and isinstance(value, dict):
            for surface_name, surface_value in value.items():
                if surface_value not in (None, [], {}, False):
                    surfaces.add(surface_name)
        elif field in {"prompt_slots", "tools", "skill_refs", "topology_fragment", "ui_panels", "safety", "budget", "persistence"}:
            if value not in (None, [], {}, False):
                surfaces.add(field)
    surface_map = {
        "prompt_slots": AttachSurface.PROMPT_SLOT_INJECTION,
        "tools": AttachSurface.TOOL_TOOLSET_ALLOWLIST,
        "skill_refs": AttachSurface.SKILL_REFERENCE,
        "topology_fragment": AttachSurface.TOPOLOGY_SUBAGENT_CONTROL,
        "ui_panels": AttachSurface.OUTCOME_EXPORT,
        "safety": AttachSurface.BUDGET_ENFORCEMENT,
        "budget": AttachSurface.BUDGET_ENFORCEMENT,
        "persistence": AttachSurface.MEMORY_SKILL_ISOLATION,
    }
    return any(contract.get(surface_map[name]).status == CapabilityStatus.DEFERRED for name in surfaces if name in surface_map)


