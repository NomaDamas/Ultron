"""Paired benchmark evaluation harness."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ultron.evolution.selection import SelectionThresholds, Selector
from ultron.module.model import EvidenceLabel


class FrozenVersions(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    hermes_version: str
    adapter_version: str
    contract_version: str
    model_provider: str
    model_name: str
    model_snapshot: str
    decoding: dict[str, Any]
    ui_registry_version: str
    baseline_module_set_hash: str

    def content_hash(self) -> str:
        canonical = self.model_dump(mode="json")
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def require_complete(self) -> None:
        for name, value in self.model_dump().items():
            if value is None or value == "" or value == {}:
                raise ValueError(f"frozen version field is required: {name}")


class GuardrailMetrics(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    cost: float = 0
    latency: float = 0
    tool_calls: int = 0
    safety_violations: int = 0
    rollback_rate: float = 0
    corrections: int = 0
    render_failures: int = 0
    permission_requests: int = 0
    privacy_violations: int = 0
    variant_count: int = 0
    composition_conflicts: int = 0
    mistaken_pruning_restores: int = 0


class PairedTask(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    task_id: str
    baseline_metric: float
    candidate_metric: float


class EvaluationReport(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    candidate_hash: str
    primitive_id: str
    frozen_versions_hash: str
    paired_tasks: int
    mean_primary_delta: float
    guardrail_breaches: list[str]
    evidence_label: EvidenceLabel
    promotable: bool
    rationale: str
    provenance: str = "manual"
    benchmark_fixture_id: str | None = None
    benchmark_task_trajectory_ids: dict[str, list[str]] = Field(default_factory=dict)


class EvaluationHarness:
    def __init__(
        self,
        selector: Selector,
        thresholds: SelectionThresholds,
    ) -> None:
        self.selector = selector
        self.thresholds = thresholds

    def evaluate_paired(
        self,
        candidate_hash: str,
        primitive_id: str,
        frozen: FrozenVersions,
        tasks: list[PairedTask],
        guardrails_before: GuardrailMetrics,
        guardrails_after: GuardrailMetrics,
        explicit_user_low_n: bool = False,
    ) -> EvaluationReport:
        frozen.require_complete()
        if not tasks:
            raise ValueError("paired evaluation requires at least one task")

        paired_count = len(tasks)
        mean_primary_delta = self._mean_primary_delta(tasks)
        baseline_metric = 1.0
        candidate_metric = 1.0 + mean_primary_delta


        selection = self.selector.evaluate(
            candidate_hash=candidate_hash,
            baseline_metric=baseline_metric,
            candidate_metric=candidate_metric,
            paired_tasks=paired_count,
            guardrails_before=guardrails_before.model_dump(),
            guardrails_after=guardrails_after.model_dump(),
            explicit_user_low_n=explicit_user_low_n,
        )
        guardrail_breaches = selection.guardrail_breaches
        evidence_label = selection.evidence_label
        promotable = selection.promotable
        rationale = selection.rationale


        return EvaluationReport(
            candidate_hash=candidate_hash,
            primitive_id=primitive_id,
            frozen_versions_hash=frozen.content_hash(),
            paired_tasks=paired_count,
            mean_primary_delta=mean_primary_delta,
            guardrail_breaches=guardrail_breaches,
            evidence_label=evidence_label,
            promotable=promotable,
            rationale=rationale,
            provenance="manual",
        )

    def _mean_primary_delta(self, tasks: list[PairedTask]) -> float:
        deltas: list[float] = []
        for task in tasks:
            if task.baseline_metric == 0:
                deltas.append(task.candidate_metric - task.baseline_metric)
            else:
                deltas.append((task.candidate_metric - task.baseline_metric) / abs(task.baseline_metric))
        return sum(deltas) / len(deltas)

