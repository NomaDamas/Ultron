"""Structured telemetry counters for Ultron runtime surfaces."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


TELEMETRY_COUNTERS = (
    "runs_started",
    "benchmarks_run",
    "promotions",
    "rollbacks",
    "prunes",
    "restores",
    "guardrail_breaches",
    "ui_render_failures",
    "permission_requests",
    "auth_failures",
    "privacy_violations",
)


class TelemetrySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs_started: int = 0
    benchmarks_run: int = 0
    promotions: int = 0
    rollbacks: int = 0
    prunes: int = 0
    restores: int = 0
    guardrail_breaches: int = 0
    ui_render_failures: int = 0
    permission_requests: int = 0
    auth_failures: int = 0
    privacy_violations: int = 0
    events: list[dict[str, str | int | float | None]] = Field(default_factory=list)


class TelemetrySink:
    """Deterministic in-memory telemetry sink with declared counters only."""

    def __init__(self) -> None:
        self._counts = {name: 0 for name in TELEMETRY_COUNTERS}
        self._events: list[dict[str, str | int | float | None]] = []

    def increment(self, counter: str, *, amount: int = 1, event: str | None = None, subject: str | None = None) -> None:
        if counter not in self._counts:
            raise KeyError(f"unknown telemetry counter: {counter}")
        self._counts[counter] += amount
        if event is not None:
            self._events.append({"event": event, "counter": counter, "amount": amount, "subject": subject})

    def snapshot(self) -> dict[str, int | list[dict[str, str | int | float | None]]]:
        payload: dict[str, int | list[dict[str, str | int | float | None]]] = {name: self._counts[name] for name in TELEMETRY_COUNTERS}
        payload["events"] = [dict(event) for event in self._events]
        return TelemetrySnapshot.model_validate(payload).model_dump(mode="json")
