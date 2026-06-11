"""Evidence-gated candidate selection."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ultron.module.model import EvidenceLabel


class SelectionThresholds(BaseModel):
    min_paired_tasks: int = 10
    min_primary_improvement: float = 0.10
    guardrail_tolerance: dict[str, float] = Field(default_factory=dict)


class SelectionOutcome(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    candidate_hash: str
    evidence_label: EvidenceLabel
    primary_delta: float
    paired_tasks: int
    guardrail_breaches: list[str]
    promotable: bool
    rationale: str


class Selector:
    def __init__(self, thresholds: SelectionThresholds) -> None:
        self.thresholds = thresholds

    def evaluate(
        self,
        candidate_hash: str,
        baseline_metric: float,
        candidate_metric: float,
        paired_tasks: int,
        guardrails_before: dict[str, float],
        guardrails_after: dict[str, float],
    ) -> SelectionOutcome:
        if baseline_metric == 0:
            primary_delta = candidate_metric - baseline_metric
        else:
            primary_delta = (candidate_metric - baseline_metric) / abs(baseline_metric)
        breaches = self._guardrail_breaches(guardrails_before, guardrails_after)
        enough_n = paired_tasks >= self.thresholds.min_paired_tasks
        enough_delta = primary_delta >= self.thresholds.min_primary_improvement
        if enough_n and enough_delta and not breaches:
            label = EvidenceLabel.BENCHMARK
            rationale = "benchmark threshold met"
        elif paired_tasks < self.thresholds.min_paired_tasks and primary_delta > 0 and not breaches:
            label = EvidenceLabel.PREFERENCE
            rationale = "positive low-N explicit user preference"
        else:
            label = EvidenceLabel.INSUFFICIENT
            rationale = "selection threshold not met"
        promotable = label in {EvidenceLabel.BENCHMARK, EvidenceLabel.CAUSAL_SUFFICIENT}
        return SelectionOutcome(
            candidate_hash=candidate_hash,
            evidence_label=label,
            primary_delta=primary_delta,
            paired_tasks=paired_tasks,
            guardrail_breaches=breaches,
            promotable=promotable,
            rationale=rationale,
        )

    def _guardrail_breaches(
        self,
        before: dict[str, float],
        after: dict[str, float],
    ) -> list[str]:
        breaches: list[str] = []
        for name, after_value in after.items():
            before_value = before.get(name, 0.0)
            tolerance = self.thresholds.guardrail_tolerance.get(name, 0.0)
            if after_value > before_value + tolerance:
                breaches.append(name)
        return sorted(breaches)
