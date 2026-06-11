from __future__ import annotations

from itertools import permutations

import pytest
from pydantic import ValidationError

from ultron.evaluation.harness import (
    EvaluationHarness,
    FrozenVersions,
    GuardrailMetrics,
    PairedTask,
)
from ultron.evolution.selection import SelectionThresholds, Selector
from ultron.feedback.channel import (
    ConsentClass,
    FeedbackChannel,
    FeedbackEvent,
    FeedbackEventType,
    SourceReliability,
    TimestampSource,
)
from ultron.module.model import EvidenceLabel


THIRTY_DAYS_SECONDS = 30 * 24 * 60 * 60


def make_event(**overrides: object) -> FeedbackEvent:
    data = {
        "event_id": "evt-1",
        "event_type": FeedbackEventType.TELEMETRY,
        "user_scope": "user-1",
        "tenant_scope": "tenant-1",
        "session_id": "session-1",
        "workflow_fingerprint": "wf-1",
        "active_module_set_id": "set-1",
        "active_module_set_hash": "set-hash",
        "module_id": None,
        "candidate_id": "candidate-1",
        "primitive_id": "primitive-1",
        "run_id": "run-1",
        "hermes_trace_id": None,
        "ui_component_id": None,
        "timestamp": 1_000.0,
        "timestamp_source": TimestampSource.SERVER,
        "consent_class": ConsentClass.OPERATIONAL,
        "source_reliability": SourceReliability.INFERRED_CLIENT,
        "redaction_status": "none",
        "retention_rule": "30d",
        "global_template_eligibility": False,
        "payload_hash": "payload-hash",
        "payload_schema": "schema-v1",
        "verifier_id": None,
    }
    data.update(overrides)
    return FeedbackEvent(**data)


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


def test_model_generated_outcome_with_verifier_is_rejected_by_constructor_and_ingest() -> None:
    with pytest.raises(ValidationError):
        make_event(
            event_type=FeedbackEventType.OUTCOME,
            source_reliability=SourceReliability.MODEL_GENERATED,
            verifier_id="verifier-1",
        )

    forged = make_event(
        event_type=FeedbackEventType.OUTCOME,
        source_reliability=SourceReliability.MODEL_GENERATED,
        verifier_id=None,
    )
    channel = FeedbackChannel()

    with pytest.raises(ValidationError):
        forged.verifier_id = "verifier-1"
    assert forged.can_verify_outcome is False
    channel.ingest(forged)
    assert channel.outcome_verifiers() == []


def test_model_generated_verifier_mutation_cannot_smuggle_outcome_verifier() -> None:
    channel = FeedbackChannel()
    event = make_event(
        event_type=FeedbackEventType.OUTCOME,
        source_reliability=SourceReliability.MODEL_GENERATED,
        verifier_id=None,
    )
    channel.ingest(event)

    with pytest.raises(ValidationError):
        event.verifier_id = "verifier-1"
    assert event.can_verify_outcome is False
    assert channel.outcome_verifiers() == []


def test_feedback_event_fields_are_immutable_after_construction() -> None:
    event = make_event()

    with pytest.raises(ValidationError):
        event.consent_class = ConsentClass.GLOBAL_TEMPLATE

def test_explicit_user_and_verified_system_outcomes_with_verifier_can_verify() -> None:
    channel = FeedbackChannel()
    explicit_user = make_event(
        event_id="explicit-user",
        event_type=FeedbackEventType.OUTCOME,
        source_reliability=SourceReliability.EXPLICIT_USER,
        verifier_id="verifier-user",
    )
    verified_system = make_event(
        event_id="verified-system",
        event_type=FeedbackEventType.OUTCOME,
        source_reliability=SourceReliability.VERIFIED_SYSTEM,
        verifier_id="verifier-system",
    )

    channel.ingest(explicit_user)
    channel.ingest(verified_system)

    assert explicit_user.can_verify_outcome is True
    assert verified_system.can_verify_outcome is True
    assert channel.outcome_verifiers() == [explicit_user, verified_system]


@pytest.mark.parametrize(
    ("consent_class", "redaction_status"),
    [
        (ConsentClass.OPERATIONAL, "redacted"),
        (ConsentClass.PRODUCT_IMPROVEMENT, "redacted"),
        (ConsentClass.RESEARCH, "redacted"),
        (ConsentClass.GLOBAL_TEMPLATE, "none"),
        (ConsentClass.GLOBAL_TEMPLATE, "private"),
        (ConsentClass.OPERATIONAL, "private"),
    ],
)
def test_global_template_eligibility_is_forced_false_without_global_consent_and_redaction(
    consent_class: ConsentClass,
    redaction_status: str,
) -> None:
    event = make_event(
        consent_class=consent_class,
        redaction_status=redaction_status,
        global_template_eligibility=True,
    )

    assert event.global_template_eligibility is False


def test_global_eligible_events_only_returns_global_template_redacted_events() -> None:
    channel = FeedbackChannel()
    eligible = make_event(
        event_id="eligible",
        consent_class=ConsentClass.GLOBAL_TEMPLATE,
        redaction_status="redacted",
        global_template_eligibility=True,
    )
    forced_private = make_event(
        event_id="forced-private",
        consent_class=ConsentClass.GLOBAL_TEMPLATE,
        redaction_status="private",
        global_template_eligibility=True,
    )
    forced_wrong_consent = make_event(
        event_id="forced-wrong-consent",
        consent_class=ConsentClass.OPERATIONAL,
        redaction_status="redacted",
        global_template_eligibility=True,
    )

    for event in [eligible, forced_private, forced_wrong_consent]:
        channel.ingest(event)

    assert eligible.global_template_eligibility is True
    assert channel.global_eligible_events() == [eligible]


def test_purge_expired_removes_ephemeral_and_stale_30d_without_lingering_reads() -> None:
    now = 100 * 24 * 60 * 60
    channel = FeedbackChannel()
    ephemeral = make_event(event_id="ephemeral", retention_rule="ephemeral", timestamp=now)
    stale_30d = make_event(
        event_id="stale-30d",
        retention_rule="30d",
        timestamp=now - THIRTY_DAYS_SECONDS - 1,
    )
    fresh_30d = make_event(
        event_id="fresh-30d",
        retention_rule="30d",
        timestamp=now - THIRTY_DAYS_SECONDS + 1,
    )
    permanent = make_event(event_id="permanent", retention_rule="permanent", timestamp=0)

    for event in [ephemeral, stale_30d, fresh_30d, permanent]:
        channel.ingest(event)

    channel.purge_expired(now=now)

    readable_ids = {event.event_id for event in channel.events_for_candidate("candidate-1")}
    assert readable_ids == {"fresh-30d", "permanent"}
    assert "ephemeral" not in readable_ids
    assert "stale-30d" not in readable_ids
    assert {event.event_id for event in channel.outcome_verifiers()} == set()
    assert {event.event_id for event in channel.global_eligible_events()} == set()


@pytest.mark.parametrize(
    "field_name",
    [
        "hermes_version",
        "adapter_version",
        "contract_version",
        "model_provider",
        "model_name",
        "model_snapshot",
        "decoding",
        "ui_registry_version",
        "baseline_module_set_hash",
    ],
)
def test_evaluation_rejects_each_empty_frozen_version_field(field_name: str) -> None:
    empty_value: object = {} if field_name == "decoding" else ""

    with pytest.raises(ValueError, match=field_name):
        harness().evaluate_paired(
            candidate_hash="candidate-hash",
            primitive_id="primitive-1",
            frozen=frozen(**{field_name: empty_value}),
            tasks=paired_tasks(10),
            guardrails_before=GuardrailMetrics(),
            guardrails_after=GuardrailMetrics(),
        )


def test_evaluation_selector_blocks_preference_and_insufficient_evidence() -> None:
    low_n_report = harness().evaluate_paired(
        candidate_hash="low-n",
        primitive_id="primitive-1",
        frozen=frozen(),
        tasks=paired_tasks(3),
        guardrails_before=GuardrailMetrics(),
        guardrails_after=GuardrailMetrics(),
    )
    low_delta_report = harness().evaluate_paired(
        candidate_hash="low-delta",
        primitive_id="primitive-1",
        frozen=frozen(),
        tasks=paired_tasks(10, candidate=10.5),
        guardrails_before=GuardrailMetrics(),
        guardrails_after=GuardrailMetrics(),
    )

    assert low_n_report.evidence_label == EvidenceLabel.PREFERENCE
    assert low_n_report.promotable is False
    assert low_delta_report.evidence_label == EvidenceLabel.INSUFFICIENT
    assert low_delta_report.promotable is False


def test_evaluation_guardrail_breach_blocks_promotion_even_when_benchmark_passes() -> None:
    report = harness().evaluate_paired(
        candidate_hash="candidate-hash",
        primitive_id="primitive-1",
        frozen=frozen(),
        tasks=paired_tasks(10),
        guardrails_before=GuardrailMetrics(latency=10, privacy_violations=0),
        guardrails_after=GuardrailMetrics(latency=14, privacy_violations=1),
    )

    assert report.evidence_label == EvidenceLabel.INSUFFICIENT
    assert report.promotable is False
    assert report.guardrail_breaches == ["privacy_violations"]


def test_evaluation_promotes_only_paired_threshold_delta_and_clean_guardrails() -> None:
    report = harness().evaluate_paired(
        candidate_hash="candidate-hash",
        primitive_id="primitive-1",
        frozen=frozen(),
        tasks=paired_tasks(10),
        guardrails_before=GuardrailMetrics(latency=10, privacy_violations=0),
        guardrails_after=GuardrailMetrics(latency=15, privacy_violations=0),
    )

    assert report.evidence_label in {EvidenceLabel.BENCHMARK, EvidenceLabel.CAUSAL_SUFFICIENT}
    assert report.promotable is True
    assert report.paired_tasks == 10
    assert report.mean_primary_delta == pytest.approx(0.2)


def test_frozen_versions_hash_is_stable_under_permuted_construction_and_nested_dict_order() -> None:
    ordered_items = [
        ("hermes_version", "hermes-1"),
        ("adapter_version", "adapter-1"),
        ("contract_version", "contract-1"),
        ("model_provider", "provider"),
        ("model_name", "model"),
        ("model_snapshot", "snapshot-1"),
        ("decoding", {"temperature": 0, "top_p": 1}),
        ("ui_registry_version", "ui-1"),
        ("baseline_module_set_hash", "baseline-hash"),
    ]
    expected_hash = FrozenVersions(**dict(ordered_items)).content_hash()

    for permuted_items in permutations(ordered_items, len(ordered_items)):
        candidate_data = dict(permuted_items)
        candidate_data["decoding"] = {"top_p": 1, "temperature": 0}
        assert FrozenVersions(**candidate_data).content_hash() == expected_hash
