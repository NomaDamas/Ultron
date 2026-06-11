"""Typed feedback events and in-memory channel controls."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FeedbackEventType(StrEnum):
    USER_CORRECTION = "user_correction"
    USER_ACCEPTANCE = "user_acceptance"
    OUTCOME = "outcome"
    TELEMETRY = "telemetry"
    RATING = "rating"


class SourceReliability(StrEnum):
    EXPLICIT_USER = "explicit_user"
    VERIFIED_SYSTEM = "verified_system"
    INFERRED_CLIENT = "inferred_client"
    MODEL_GENERATED = "model_generated"


class ConsentClass(StrEnum):
    OPERATIONAL = "operational"
    PRODUCT_IMPROVEMENT = "product_improvement"
    GLOBAL_TEMPLATE = "global_template"
    RESEARCH = "research"


class TimestampSource(StrEnum):
    SERVER = "server"
    CLIENT = "client"
    HERMES = "hermes"
    VERIFIER = "verifier"


class FeedbackEvent(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    event_id: str
    event_type: FeedbackEventType
    user_scope: str
    tenant_scope: str
    session_id: str
    workflow_fingerprint: str
    active_module_set_id: str
    active_module_set_hash: str
    module_id: str | None
    candidate_id: str | None
    primitive_id: str | None
    run_id: str
    hermes_trace_id: str | None
    ui_component_id: str | None
    timestamp: float
    timestamp_source: TimestampSource
    consent_class: ConsentClass
    source_reliability: SourceReliability
    redaction_status: str = Field(pattern="^(none|redacted|private)$")
    retention_rule: str
    global_template_eligibility: bool = False
    payload_hash: str
    payload_schema: str
    verifier_id: str | None = None

    @property
    def can_verify_outcome(self) -> bool:
        return (
            self.source_reliability != SourceReliability.MODEL_GENERATED
            and self.event_type == FeedbackEventType.OUTCOME
            and self.verifier_id is not None
        )

    @model_validator(mode="after")
    def _enforce_verifier_and_global_eligibility(self) -> "FeedbackEvent":
        if (
            self.event_type == FeedbackEventType.OUTCOME
            and self.verifier_id is not None
            and self.source_reliability == SourceReliability.MODEL_GENERATED
        ):
            raise ValueError("model-generated feedback cannot verify outcomes")
        if self.global_template_eligibility and not (
            self.consent_class == ConsentClass.GLOBAL_TEMPLATE
            and self.redaction_status == "redacted"
        ):
            self.global_template_eligibility = False
        return self


class FeedbackChannel:
    def __init__(self) -> None:
        self._events: list[FeedbackEvent] = []

    def ingest(self, event: FeedbackEvent) -> FeedbackEvent:
        if (
            event.event_type == FeedbackEventType.OUTCOME
            and event.verifier_id is not None
            and event.source_reliability == SourceReliability.MODEL_GENERATED
        ):
            raise ValueError("model-generated feedback cannot verify outcomes")
        self._events.append(event)
        return event

    def events_for_candidate(self, candidate_id: str) -> list[FeedbackEvent]:
        return [event for event in self._events if event.candidate_id == candidate_id]

    def outcome_verifiers(self) -> list[FeedbackEvent]:
        return [event for event in self._events if event.can_verify_outcome]

    def purge_expired(self, now: float) -> None:
        thirty_days_seconds = 30 * 24 * 60 * 60
        retained: list[FeedbackEvent] = []
        for event in self._events:
            if event.retention_rule == "ephemeral":
                continue
            if event.retention_rule == "30d" and now - event.timestamp > thirty_days_seconds:
                continue
            retained.append(event)
        self._events = retained

    def global_eligible_events(self) -> list[FeedbackEvent]:
        return [event for event in self._events if event.global_template_eligibility]
