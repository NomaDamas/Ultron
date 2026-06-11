from __future__ import annotations

import pytest
from pydantic import ValidationError

from ultron.feedback.channel import (
    ConsentClass,
    FeedbackChannel,
    FeedbackEvent,
    FeedbackEventType,
    SourceReliability,
    TimestampSource,
)


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


def test_model_generated_event_as_outcome_verifier_rejected() -> None:
    with pytest.raises(ValidationError):
        make_event(
            event_type=FeedbackEventType.OUTCOME,
            source_reliability=SourceReliability.MODEL_GENERATED,
            verifier_id="verifier-1",
        )


def test_verified_system_outcome_verifier_accepted() -> None:
    channel = FeedbackChannel()
    event = make_event(
        event_type=FeedbackEventType.OUTCOME,
        source_reliability=SourceReliability.VERIFIED_SYSTEM,
        verifier_id="verifier-1",
    )

    channel.ingest(event)

    assert event.can_verify_outcome is True
    assert channel.outcome_verifiers() == [event]


def test_global_template_eligibility_forced_false_unless_global_template_redacted() -> None:
    unredacted = make_event(global_template_eligibility=True)
    eligible = make_event(
        event_id="evt-2",
        consent_class=ConsentClass.GLOBAL_TEMPLATE,
        redaction_status="redacted",
        global_template_eligibility=True,
    )
    private = make_event(
        event_id="evt-3",
        consent_class=ConsentClass.GLOBAL_TEMPLATE,
        redaction_status="private",
        global_template_eligibility=True,
    )
    channel = FeedbackChannel()
    channel.ingest(unredacted)
    channel.ingest(eligible)
    channel.ingest(private)

    assert unredacted.global_template_eligibility is False
    assert private.global_template_eligibility is False
    assert channel.global_eligible_events() == [eligible]


def test_purge_expired_drops_ephemeral_and_30d_expired_but_keeps_permanent() -> None:
    channel = FeedbackChannel()
    expired = make_event(event_id="expired", timestamp=0.0, retention_rule="30d")
    fresh = make_event(event_id="fresh", timestamp=2 * 24 * 60 * 60, retention_rule="30d")
    ephemeral = make_event(event_id="ephemeral", retention_rule="ephemeral")
    permanent = make_event(event_id="permanent", retention_rule="permanent")
    for event in [expired, fresh, ephemeral, permanent]:
        channel.ingest(event)

    channel.purge_expired(now=31 * 24 * 60 * 60)

    assert channel.events_for_candidate("candidate-1") == [fresh, permanent]


def test_outcome_verifiers_excludes_model_generated_non_verifier() -> None:
    channel = FeedbackChannel()
    model_outcome = make_event(
        event_type=FeedbackEventType.OUTCOME,
        source_reliability=SourceReliability.MODEL_GENERATED,
        verifier_id=None,
    )
    verified = make_event(
        event_id="verified",
        event_type=FeedbackEventType.OUTCOME,
        source_reliability=SourceReliability.VERIFIED_SYSTEM,
        verifier_id="verifier-1",
    )
    channel.ingest(model_outcome)
    channel.ingest(verified)

    assert channel.outcome_verifiers() == [verified]
