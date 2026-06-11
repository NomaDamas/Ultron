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
        if primitive is VariationPrimitive.TOPOLOGY_CHANGE:
            spec = self.adapter_contract.get(AttachSurface.TOPOLOGY_SUBAGENT_CONTROL)
            if spec.status == CapabilityStatus.DEFERRED:
                raise ValueError("topology change rejected: adapter topology surface is deferred")

        parent = self.registry.get(parent_hash).module
        candidate = self._candidate_from_change(parent, primitive, change)
        requires_human_approval = bool(approval_only) or not self._candidate_can_auto_promote(candidate)
        if primitive is VariationPrimitive.TOPOLOGY_CHANGE:
            requires_human_approval = True
        rationale = f"Apply {primitive.value} to {', '.join(sorted(touched))}"
        return MutationProposal(
            parent_hash=parent_hash,
            primitive=primitive,
            change=dict(change),
            rationale=rationale,
            requires_human_approval=requires_human_approval,
            human_approved=human_approved,
        )

    def apply(self, proposal: MutationProposal) -> HarnessModule:
        proposal = MutationProposal.model_validate(proposal)
        if proposal.requires_human_approval and not proposal.human_approved:
            raise ValueError("mutation proposal requires human approval")
        parent = self.registry.get(proposal.parent_hash).module
        candidate = self._candidate_from_change(parent, proposal.primitive, proposal.change)
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
        if primitive is VariationPrimitive.TOPOLOGY_CHANGE:
            candidate.validate_surfaces(self.adapter_contract)
        return candidate

    def _candidate_can_auto_promote(self, candidate: HarnessModule) -> bool:
        parent = self.registry.get(candidate.parent_id).module if candidate.parent_id else None
        if parent is None:
            return True
        if not set(candidate.surfaces.tools).issubset(set(parent.surfaces.tools)):
            return False
        candidate_surfaces = _declared_surface_names(candidate)
        parent_surfaces = _declared_surface_names(parent)
        if not candidate_surfaces.issubset(parent_surfaces):
            return False
        if not set(candidate.required_adapter_capabilities).issubset(set(parent.required_adapter_capabilities)):
            return False
        return _persistence_rank(candidate.persistence_policy) <= _persistence_rank(parent.persistence_policy)



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


def _persistence_rank(policy: Any) -> int:
    return {
        "READ_ONLY": 0,
        "ISOLATED": 1,
        "CHECKPOINTED": 2,
        "NORMAL": 3,
    }[policy.value if hasattr(policy, "value") else str(policy)]
