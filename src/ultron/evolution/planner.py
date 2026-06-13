"""Deterministic one-primitive variation planning."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ultron.evolution.variation import MutationProposal, VariationPrimitive
from ultron.feedback.aggregation import FeedbackSummary
from ultron.module.model import HarnessModule


class VariationPlanConstraints(BaseModel):
    max_variants: int = Field(default=1, ge=0)
    existing_variants: int = Field(default=0, ge=0)
    allow_permission_expansion: bool = False
    indicated_tools: list[str] = Field(default_factory=list)
    touch_topology: bool = False
    compound_changes: dict[str, Any] = Field(default_factory=dict)


class PendingVariationApproval(BaseModel):
    reason: str
    primitive: VariationPrimitive | None = None
    change: dict[str, Any] = Field(default_factory=dict)


class VariationPlanner:
    def plan(
        self,
        parent_module: HarnessModule,
        feedback_summary: FeedbackSummary | None,
        eval_summary: dict[str, Any] | None,
        constraints: VariationPlanConstraints,
    ) -> MutationProposal | PendingVariationApproval:
        if constraints.existing_variants >= constraints.max_variants:
            return PendingVariationApproval(reason="variant budget exhausted")
        indicated = set(constraints.indicated_tools)
        if indicated and not indicated.issubset(set(parent_module.surfaces.tools)):
            return PendingVariationApproval(
                reason="permission expansion requires human approval",
                primitive=VariationPrimitive.TOOLSET_TOGGLE,
                change={"surfaces.tools": sorted(set(parent_module.surfaces.tools) | indicated)},
            )
        if constraints.touch_topology:
            return PendingVariationApproval(reason="topology changes require human approval", primitive=VariationPrimitive.TOPOLOGY_CHANGE, change={"topology_fragment_hash": "pending"})
        if len(constraints.compound_changes) > 1:
            return PendingVariationApproval(reason="compound variation requires human approval", change=dict(constraints.compound_changes))

        metric = float((eval_summary or {}).get("primary_metric", (eval_summary or {}).get("mean_primary_delta", 0.0)))
        rating = feedback_summary.mean_rating if feedback_summary is not None else None
        if metric < 0 or (rating is not None and rating < 3):
            change = {"prompt_pack_hash": self._prompt_revision(parent_module, feedback_summary, eval_summary)}
            primitive = VariationPrimitive.PROMPT_SLOT_EDIT
        elif parent_module.surfaces.ui_panels:
            change = {"ui_panel_contract_hash": parent_module.surfaces.ui_panels[0]}
            primitive = VariationPrimitive.UI_PANEL_PRIORITY
        else:
            max_tool_calls = int((parent_module.surfaces.budget or {}).get("max_tool_calls", 1))
            change = {"budget.max_tool_calls": max(1, max_tool_calls - 1)}
            primitive = VariationPrimitive.BUDGET_TIGHTEN
        return MutationProposal(
            parent_hash=parent_module.content_hash or "",
            primitive=primitive,
            change=change,
            rationale=f"Planner selected one bounded {primitive.value} change",
            requires_human_approval=False,
            human_approved=False,
        )

    def _prompt_revision(self, parent_module: HarnessModule, feedback_summary: FeedbackSummary | None, eval_summary: dict[str, Any] | None) -> str:
        rating = feedback_summary.mean_rating if feedback_summary is not None else None
        metric = (eval_summary or {}).get("primary_metric", (eval_summary or {}).get("mean_primary_delta", 0.0))
        return f"tighten triage guidance for {parent_module.module_id}; metric={metric}; rating={rating}"
