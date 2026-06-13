"""Feedback aggregation into non-promotable preference signals."""

from __future__ import annotations

from collections import Counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ultron.feedback.channel import FeedbackChannel, FeedbackEvent, FeedbackEventType, SourceReliability
from ultron.module.model import EvidenceLabel


COUNTED_RELIABILITY = {SourceReliability.EXPLICIT_USER, SourceReliability.VERIFIED_SYSTEM}


class FeedbackSummary(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    candidate_hash: str
    n_events: int = 0
    explicit_user_corrections: int = 0
    explicit_user_acceptances: int = 0
    mean_rating: float | None = None
    preference_signal: bool = False
    evidence_label: EvidenceLabel = EvidenceLabel.INSUFFICIENT
    reliability_breakdown: dict[str, int] = Field(default_factory=dict)


class FeedbackAggregator:
    """Derive candidate preference summaries from retained feedback events."""

    def __init__(self, channel: FeedbackChannel) -> None:
        self.channel = channel

    def summarize(self, candidate_hash: str) -> FeedbackSummary:
        events = [event for event in self.channel.events_for_candidate(candidate_hash) if _counts_toward_preference(event)]
        ratings = [_rating_value(event) for event in events]
        ratings = [rating for rating in ratings if rating is not None]
        corrections = sum(1 for event in events if event.event_type is FeedbackEventType.USER_CORRECTION)
        acceptances = sum(1 for event in events if event.event_type is FeedbackEventType.USER_ACCEPTANCE)
        mean_rating = sum(ratings) / len(ratings) if ratings else None
        preference_signal = bool(acceptances > 0 or corrections > 0 or (mean_rating is not None and mean_rating > 0))
        return FeedbackSummary(
            candidate_hash=candidate_hash,
            n_events=len(events),
            explicit_user_corrections=corrections,
            explicit_user_acceptances=acceptances,
            mean_rating=mean_rating,
            preference_signal=preference_signal,
            evidence_label=EvidenceLabel.PREFERENCE if preference_signal else EvidenceLabel.INSUFFICIENT,
            reliability_breakdown=dict(Counter(event.source_reliability.value for event in events)),
        )


# FeedbackEvent intentionally stores only a privacy-preserving payload hash. submit_feedback
# uses the canonical schema-specific hash below, so ratings can be recovered for first-party
# events without retaining raw comments.
def canonical_rating_payload(rating: int, comment: str) -> dict[str, Any]:
    return {"comment": comment.strip(), "rating": int(rating)}


def _counts_toward_preference(event: FeedbackEvent) -> bool:
    return event.source_reliability in COUNTED_RELIABILITY


def _rating_value(event: FeedbackEvent) -> float | None:
    if event.event_type is not FeedbackEventType.RATING:
        return None
    schema = event.payload_schema
    if schema.startswith("rating:v1:"):
        try:
            return float(schema.rsplit(":", 1)[1])
        except ValueError:
            return None
    return None
