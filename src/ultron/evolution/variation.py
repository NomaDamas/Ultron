"""Bounded one-primitive canary variation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from ultron.module.blobs import BlobKind, BlobStore, BudgetPolicyBlob, PromptPack, SafetyPolicyBlob, ToolPolicyBlob, UiPanelContract

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
    def __init__(self, registry: ModuleRegistry, adapter_contract: Any, blob_store: BlobStore | None = None) -> None:
        self.registry = registry
        self.adapter_contract = adapter_contract
        self.blob_store = blob_store or registry.blob_store

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
        realized_change = self._realize_blob_change(parent, primitive, change)
        for field, value in realized_change.items():
            _set_candidate_field(data, field, value)
        candidate = HarnessModule.model_validate(data).finalized()
        candidate.validate_surfaces(self.adapter_contract)
        return candidate

    def _realize_blob_change(
        self,
        parent: HarnessModule,
        primitive: VariationPrimitive,
        change: dict[str, Any],
    ) -> dict[str, Any]:
        if self.blob_store is None:
            return dict(change)
        if len(change) != 1:
            return dict(change)
        field, value = next(iter(change.items()))
        if primitive is VariationPrimitive.PROMPT_SLOT_EDIT:
            return self._realize_prompt_pack_edit(parent, field, value)
        if primitive is VariationPrimitive.TOOLSET_TOGGLE:
            return self._realize_tool_policy_edit(parent, field, value)
        if primitive is VariationPrimitive.UI_PANEL_PRIORITY:
            return self._realize_ui_panel_edit(parent, field, value)
        if primitive is VariationPrimitive.BUDGET_TIGHTEN:
            return self._realize_budget_policy_edit(parent, field, value)
        if primitive is VariationPrimitive.SAFETY_TIGHTEN:
            return self._realize_safety_policy_edit(parent, field, value)
        return dict(change)

    def _realize_prompt_pack_edit(self, parent: HarnessModule, field: str, value: Any) -> dict[str, Any]:
        parent_hash = self._required_parent_ref(parent.prompt_pack_hash, BlobKind.PROMPT_PACK)
        parent_pack = self.blob_store.get_typed(BlobKind.PROMPT_PACK, parent_hash, PromptPack)
        slots = dict(parent_pack.slots)
        if field == "prompt_pack_hash":
            text = str(value)
            if not text:
                raise ValueError("prompt slot edit cannot be empty")
            slot = next(iter(slots), "default")
            slots[slot] = text
        elif field in {"prompt_slots", "surfaces.prompt_slots"}:
            requested = list(value)
            slots = {slot: slots.get(slot, parent_pack.slots.get(slot, "")) for slot in requested}
        else:
            return {field: value}
        new_pack = PromptPack(slots=slots, notes=parent_pack.notes)
        new_hash = self.blob_store.put(BlobKind.PROMPT_PACK, new_pack)
        return {"prompt_pack_hash": new_hash}

    def _realize_tool_policy_edit(self, parent: HarnessModule, field: str, value: Any) -> dict[str, Any]:
        parent_hash = self._required_parent_ref(parent.tool_allowlist_hash, BlobKind.TOOL_POLICY)
        parent_policy = self.blob_store.get_typed(BlobKind.TOOL_POLICY, parent_hash, ToolPolicyBlob)
        tools = list(value) if field in {"tools", "surfaces.tools"} else list(parent_policy.tools)
        if field == "tool_allowlist_hash":
            tool = str(value)
            if tool in tools:
                tools.remove(tool)
            else:
                tools.append(tool)
        elif field not in {"tools", "surfaces.tools"}:
            return {field: value}
        new_hash = self.blob_store.put(BlobKind.TOOL_POLICY, ToolPolicyBlob(tools=tools, rationale=parent_policy.rationale))
        return {"tool_allowlist_hash": new_hash, "surfaces.tools": tools}

    def _realize_ui_panel_edit(self, parent: HarnessModule, field: str, value: Any) -> dict[str, Any]:
        parent_hash = self._required_parent_ref(parent.ui_panel_contract_hash, BlobKind.UI_PANEL_CONTRACT)
        parent_contract = self.blob_store.get_typed(BlobKind.UI_PANEL_CONTRACT, parent_hash, UiPanelContract)
        panels = list(value) if field in {"ui_panels", "surfaces.ui_panels"} else list(parent_contract.panels)
        if field == "ui_panel_contract_hash":
            panel = str(value)
            panels = [item for item in panels if item != panel]
            panels.insert(0, panel)
        elif field not in {"ui_panels", "surfaces.ui_panels"}:
            return {field: value}
        new_hash = self.blob_store.put(BlobKind.UI_PANEL_CONTRACT, UiPanelContract(panels=panels, notes=parent_contract.notes))
        return {"ui_panel_contract_hash": new_hash, "surfaces.ui_panels": panels}

    def _realize_budget_policy_edit(self, parent: HarnessModule, field: str, value: Any) -> dict[str, Any]:
        parent_hash = self._required_parent_ref(parent.budget_policy_hash, BlobKind.BUDGET_POLICY)
        parent_budget = self.blob_store.get_typed(BlobKind.BUDGET_POLICY, parent_hash, BudgetPolicyBlob)
        max_tool_calls = int(value["max_tool_calls"] if isinstance(value, dict) and "max_tool_calls" in value else value)
        if max_tool_calls > parent_budget.max_tool_calls:
            raise ValueError("budget tighten cannot increase max_tool_calls")
        new_budget = parent_budget.model_copy(update={"max_tool_calls": max_tool_calls})
        new_hash = self.blob_store.put(BlobKind.BUDGET_POLICY, new_budget)
        return {"budget_policy_hash": new_hash, "surfaces.budget": {"max_tool_calls": max_tool_calls}}

    def _realize_safety_policy_edit(self, parent: HarnessModule, field: str, value: Any) -> dict[str, Any]:
        parent_hash = self._required_parent_ref(parent.safety_policy_hash, BlobKind.SAFETY_POLICY)
        parent_safety = self.blob_store.get_typed(BlobKind.SAFETY_POLICY, parent_hash, SafetyPolicyBlob)
        updates = value if isinstance(value, dict) else {"extra_rules": {str(value): "tightened"}}
        new_safety = parent_safety.model_copy(update=updates)
        new_hash = self.blob_store.put(BlobKind.SAFETY_POLICY, new_safety)
        return {"safety_policy_hash": new_hash, "surfaces.safety": new_safety.model_dump(mode="json")}

    def _required_parent_ref(self, content_hash: str | None, kind: BlobKind) -> str:
        if self.blob_store is None:
            raise ValueError(f"blob store required for {kind.value} variation")
        if content_hash is None or not self.blob_store.has(kind, content_hash):
            raise ValueError(f"missing parent blob for {kind.value}: {content_hash}")
        return content_hash
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


