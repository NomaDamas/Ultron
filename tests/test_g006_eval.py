from __future__ import annotations

import pytest

from ultron.evaluation.harness import (
    EvaluationHarness,
    FrozenVersions,
    GuardrailMetrics,
    PairedTask,
)
from ultron.evolution.selection import SelectionThresholds, Selector
from ultron.module.model import EvidenceLabel


def frozen(**overrides: object) -> FrozenVersions:
    data = {
        "hermes_version": "hermes-1",
        "adapter_version": "adapter-1",
        "contract_version": "contract-1",
        "model_provider": "provider",
        "model_name": "model",
        "model_snapshot": "snapshot-1",
        "decoding": {"temperature": 0, "top_p": 1},
        "ui_registry_version": "ui-1",
        "baseline_module_set_hash": "baseline-hash",
    }
    data.update(overrides)
    return FrozenVersions(**data)


def harness() -> EvaluationHarness:
    thresholds = SelectionThresholds(
        min_paired_tasks=10,
        min_primary_improvement=0.10,
        guardrail_tolerance={"latency": 5, "privacy_violations": 0},
    )
    return EvaluationHarness(
        selector=Selector(thresholds),
        thresholds=thresholds,
    )


def paired_tasks(count: int, baseline: float = 10.0, candidate: float = 12.0) -> list[PairedTask]:
    return [
        PairedTask(task_id=f"task-{index}", baseline_metric=baseline, candidate_metric=candidate)
        for index in range(count)
    ]


def test_paired_threshold_and_no_breach_is_promotable_via_selector() -> None:
    report = harness().evaluate_paired(
        candidate_hash="candidate-hash",
        primitive_id="primitive-1",
        frozen=frozen(),
        tasks=paired_tasks(10),
        guardrails_before=GuardrailMetrics(latency=10),
        guardrails_after=GuardrailMetrics(latency=14),
    )

    assert report.evidence_label in {EvidenceLabel.BENCHMARK, EvidenceLabel.CAUSAL_SUFFICIENT}
    assert report.promotable is True
    assert report.paired_tasks == 10
    assert report.mean_primary_delta == pytest.approx(0.2)


def test_guardrail_breach_is_not_promotable() -> None:
    report = harness().evaluate_paired(
        candidate_hash="candidate-hash",
        primitive_id="primitive-1",
        frozen=frozen(),
        tasks=paired_tasks(10),
        guardrails_before=GuardrailMetrics(latency=10),
        guardrails_after=GuardrailMetrics(latency=20),
    )

    assert report.promotable is False
    assert report.evidence_label == EvidenceLabel.INSUFFICIENT
    assert report.guardrail_breaches == ["latency"]


def test_guardrail_breach_uses_selector_tolerance_not_harness_limits() -> None:
    report = harness().evaluate_paired(
        candidate_hash="candidate-hash",
        primitive_id="primitive-1",
        frozen=frozen(),
        tasks=paired_tasks(10),
        guardrails_before=GuardrailMetrics(latency=10),
        guardrails_after=GuardrailMetrics(latency=16),
    )

    assert report.promotable is False
    assert report.evidence_label == EvidenceLabel.INSUFFICIENT
    assert report.guardrail_breaches == ["latency"]


def test_low_n_explicit_user_canary_is_preference_not_promotable() -> None:
    report = harness().evaluate_paired(
        candidate_hash="candidate-hash",
        primitive_id="primitive-1",
        frozen=frozen(),
        tasks=paired_tasks(3),
        guardrails_before=GuardrailMetrics(),
        guardrails_after=GuardrailMetrics(),
        explicit_user_low_n=True,
    )

    assert report.evidence_label == EvidenceLabel.PREFERENCE
    assert report.promotable is False


def test_missing_frozen_version_field_raises() -> None:
    with pytest.raises(ValueError):
        harness().evaluate_paired(
            candidate_hash="candidate-hash",
            primitive_id="primitive-1",
            frozen=frozen(hermes_version=""),
            tasks=paired_tasks(10),
            guardrails_before=GuardrailMetrics(),
            guardrails_after=GuardrailMetrics(),
        )


def test_frozen_versions_hash_is_deterministic() -> None:
    first = frozen(decoding={"temperature": 0, "top_p": 1})
    second = frozen(decoding={"top_p": 1, "temperature": 0})

    assert first.content_hash() == second.content_hash()
