from ultron.evolution.selection import SelectionThresholds, Selector
from ultron.module.model import EvidenceLabel


def test_benchmark_threshold_is_promotable():
    outcome = Selector(SelectionThresholds()).evaluate(
        "h1", 1.0, 1.12, 10, {"errors": 0.0}, {"errors": 0.0}
    )

    assert outcome.evidence_label == EvidenceLabel.BENCHMARK
    assert outcome.promotable is True


def test_low_n_explicit_user_preference_is_not_promotable():
    outcome = Selector(SelectionThresholds()).evaluate("h1", 1.0, 1.2, 3, {}, {})

    assert outcome.evidence_label == EvidenceLabel.PREFERENCE
    assert outcome.promotable is False


def test_guardrail_breach_is_insufficient_not_promotable():
    outcome = Selector(SelectionThresholds(guardrail_tolerance={"errors": 0.1})).evaluate(
        "h1", 1.0, 1.2, 12, {"errors": 1.0}, {"errors": 1.2}
    )

    assert outcome.guardrail_breaches == ["errors"]
    assert outcome.evidence_label == EvidenceLabel.INSUFFICIENT
    assert outcome.promotable is False


def test_sub_threshold_delta_is_not_promotable():
    outcome = Selector(SelectionThresholds()).evaluate("h1", 1.0, 1.05, 20, {}, {})

    assert outcome.evidence_label == EvidenceLabel.INSUFFICIENT
    assert outcome.promotable is False
