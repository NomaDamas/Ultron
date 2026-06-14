"""Server-owned generative UI runtime and typed action validation."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_COMPONENTS = 24
MAX_SUMMARY_LINES = 8
MAX_EVIDENCE_LABELS = 8
MAX_GATED_ACTIONS = 12
MAX_COMPONENT_ACTIONS = 12
MAX_TELEMETRY_SCHEMA_ITEMS = 8
MAX_TELEMETRY_SCHEMA_ENTRY_LENGTH = 80
MAX_SIGNAL_KEYS = 12
NON_MOTION_ANIMATION_KINDS: set["AnimationKind"] = set()


class ComponentType(StrEnum):
    PLAN_PANEL = "PLAN_PANEL"
    RISK_PANEL = "RISK_PANEL"
    TEST_PANEL = "TEST_PANEL"
    FEEDBACK_PANEL = "FEEDBACK_PANEL"
    TRACE_PANEL = "TRACE_PANEL"
    MUTATION_DIFF_PANEL = "MUTATION_DIFF_PANEL"
    APPROVAL_PANEL = "APPROVAL_PANEL"
    ROLLBACK_PANEL = "ROLLBACK_PANEL"
    INTAKE_PANEL = "INTAKE_PANEL"
    CONTEXT_PANEL = "CONTEXT_PANEL"
    RUN_SUMMARY_CARD = "RUN_SUMMARY_CARD"
    TOOL_RESULT_CARD = "TOOL_RESULT_CARD"
    HARNESS_EVOLUTION_CARD = "HARNESS_EVOLUTION_CARD"
    EVIDENCE_STATUS_CARD = "EVIDENCE_STATUS_CARD"
    PERSONALIZATION_SIGNAL_CARD = "PERSONALIZATION_SIGNAL_CARD"
    SAFETY_STATUS_CARD = "SAFETY_STATUS_CARD"
    ORB_STATUS = "ORB_STATUS"
    TIMELINE_STEP = "TIMELINE_STEP"


class Region(StrEnum):
    SIDEBAR = "sidebar"
    MAIN = "main"
    DETAILS = "details"
    ACTIONS = "actions"


class ActionType(StrEnum):
    SUBMIT_REQUEST = "SUBMIT_REQUEST"
    GIVE_FEEDBACK = "GIVE_FEEDBACK"
    APPROVE_PROMOTION = "APPROVE_PROMOTION"
    RUN_BENCHMARK = "RUN_BENCHMARK"
    ROLLBACK_CANARY = "ROLLBACK_CANARY"
    RESTORE_MODULE = "RESTORE_MODULE"
    REQUEST_PERMISSION_EXPANSION = "REQUEST_PERMISSION_EXPANSION"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REDACTED = "redacted"


class HarnessLifecycle(StrEnum):
    SEED = "seed"
    CANDIDATE = "candidate"
    CANARY = "canary"
    SURVIVOR = "survivor"
    QUARANTINED = "quarantined"
    PRUNED = "pruned"


class RollbackState(StrEnum):
    READY = "ready"
    PENDING = "pending"
    ROLLING_BACK = "rolling_back"
    COMPLETE = "complete"
    UNAVAILABLE = "unavailable"


class OrbState(StrEnum):
    IDLE = "idle"
    THINKING = "thinking"
    BUILDING = "building"
    ERROR = "error"
    VOICE_UNAVAILABLE = "voice_unavailable"


class TimelineStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETE = "complete"
    ERROR = "error"
    SKIPPED = "skipped"


class AnimationKind(StrEnum):
    NONE = "none"
    FADE_IN = "fade_in"
    SLIDE_UP = "slide_up"
    PULSE_GLOW = "pulse_glow"
    RETICLE_SCAN = "reticle_scan"
    EXPAND = "expand"


NON_MOTION_ANIMATION_KINDS = {AnimationKind.NONE, AnimationKind.FADE_IN, AnimationKind.PULSE_GLOW}


class AnimationHint(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    kind: AnimationKind
    duration_ms: int = Field(ge=0, le=1200)
    delay_ms: int = Field(ge=0, le=1000)
    reduced_motion_fallback: AnimationKind | None = None

    @model_validator(mode="after")
    def validate_fallback(self) -> "AnimationHint":
        if self.kind == AnimationKind.NONE:
            return self
        if self.reduced_motion_fallback is None:
            raise ValueError("reduced_motion_fallback is required for animated hints")
        if self.reduced_motion_fallback not in NON_MOTION_ANIMATION_KINDS:
            raise ValueError("reduced_motion_fallback must be NONE or a non-motion kind")
        return self



class PanelProps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    panel: str | None = Field(default=None, min_length=1, max_length=128)
    manifest_hash: str | None = Field(default=None, max_length=128)
    actions: list[ActionType] = Field(default_factory=list, max_length=MAX_COMPONENT_ACTIONS)

    @field_validator("actions", mode="before")
    @classmethod
    def validate_actions(cls, value: Any) -> list[ActionType]:
        return validate_declared_actions(value)


class RunSummaryProps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=80)
    workflow: str = Field(min_length=1, max_length=80)
    manifest_hash: str = Field(min_length=1, max_length=128)
    trajectory_id: str = Field(min_length=1, max_length=80)
    status: RunStatus
    summary_lines: list[str] = Field(default_factory=list, max_length=MAX_SUMMARY_LINES)

    @field_validator("summary_lines")
    @classmethod
    def validate_summary_lines(cls, value: list[str]) -> list[str]:
        return _bounded_strings(value, max_items=MAX_SUMMARY_LINES, max_length=180, field_name="summary_lines")


class ToolResultProps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=80)
    status: ToolStatus
    output_summary: list[str] = Field(default_factory=list, max_length=MAX_SUMMARY_LINES)
    output_redacted: bool
    secrets_redacted: bool

    @field_validator("output_summary")
    @classmethod
    def validate_output_summary(cls, value: list[str]) -> list[str]:
        return _bounded_strings(value, max_items=MAX_SUMMARY_LINES, max_length=180, field_name="output_summary")


class HarnessEvolutionProps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_hash: str = Field(min_length=1, max_length=32)
    candidate_hash: str = Field(min_length=1, max_length=32)
    primitive: str = Field(min_length=1, max_length=80)
    lifecycle: HarnessLifecycle
    rationale: str = Field(min_length=1, max_length=240)
    canary_id: str | None = Field(default=None, max_length=80)


class EvidenceStatusProps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provenance: str = Field(min_length=1, max_length=120)
    promotable: bool
    evidence_label: str = Field(min_length=1, max_length=80)
    paired_tasks: int = Field(ge=0, le=100)


class PersonalizationSignalProps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_counts: dict[str, int] = Field(default_factory=dict)
    evidence_labels: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_LABELS)
    summary_hash: str = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=1, max_length=240)

    @field_validator("signal_counts")
    @classmethod
    def validate_signal_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if len(value) > MAX_SIGNAL_KEYS:
            raise ValueError("signal_counts has too many entries")
        for key, count in value.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 40:
                raise ValueError("signal_counts keys must be bounded strings")
            if not isinstance(count, int) or count < 0 or count > 1000:
                raise ValueError("signal_counts values must be bounded non-negative integers")
        return value

    @field_validator("evidence_labels")
    @classmethod
    def validate_evidence_labels(cls, value: list[str]) -> list[str]:
        return _bounded_strings(value, max_items=MAX_EVIDENCE_LABELS, max_length=80, field_name="evidence_labels")


class SafetyStatusProps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pending_permissions: int = Field(ge=0, le=100)
    rollback_state: RollbackState
    no_poisoning_ok: bool
    gated_actions: list[str] = Field(default_factory=list, max_length=MAX_GATED_ACTIONS)

    @field_validator("gated_actions")
    @classmethod
    def validate_gated_actions(cls, value: list[str]) -> list[str]:
        return _bounded_strings(value, max_items=MAX_GATED_ACTIONS, max_length=80, field_name="gated_actions")


class OrbStatusProps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: OrbState
    status_text: str = Field(min_length=1, max_length=120)


class TimelineStepProps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    status: TimelineStatus
    detail: str = Field(min_length=1, max_length=180)


COMPONENT_PROP_MODELS: dict[ComponentType, type[BaseModel]] = {
    ComponentType.PLAN_PANEL: PanelProps,
    ComponentType.RISK_PANEL: PanelProps,
    ComponentType.TEST_PANEL: PanelProps,
    ComponentType.FEEDBACK_PANEL: PanelProps,
    ComponentType.TRACE_PANEL: PanelProps,
    ComponentType.MUTATION_DIFF_PANEL: PanelProps,
    ComponentType.APPROVAL_PANEL: PanelProps,
    ComponentType.ROLLBACK_PANEL: PanelProps,
    ComponentType.INTAKE_PANEL: PanelProps,
    ComponentType.CONTEXT_PANEL: PanelProps,
    ComponentType.RUN_SUMMARY_CARD: RunSummaryProps,
    ComponentType.TOOL_RESULT_CARD: ToolResultProps,
    ComponentType.HARNESS_EVOLUTION_CARD: HarnessEvolutionProps,
    ComponentType.EVIDENCE_STATUS_CARD: EvidenceStatusProps,
    ComponentType.PERSONALIZATION_SIGNAL_CARD: PersonalizationSignalProps,
    ComponentType.SAFETY_STATUS_CARD: SafetyStatusProps,
    ComponentType.ORB_STATUS: OrbStatusProps,
    ComponentType.TIMELINE_STEP: TimelineStepProps,
}

PRIVILEGED_ACTIONS: set[ActionType] = {
    ActionType.APPROVE_PROMOTION,
    ActionType.RUN_BENCHMARK,
    ActionType.ROLLBACK_CANARY,
    ActionType.RESTORE_MODULE,
    ActionType.REQUEST_PERMISSION_EXPANSION,
}


class UiComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    type: ComponentType
    region: Region
    priority: int
    props: dict[str, Any] = Field(default_factory=dict)
    telemetry_schema: list[str] = Field(default_factory=list)
    animation: AnimationHint | None = None

    @field_validator("telemetry_schema")
    @classmethod
    def validate_telemetry_schema(cls, value: list[str]) -> list[str]:
        return _bounded_strings(
            value,
            max_items=MAX_TELEMETRY_SCHEMA_ITEMS,
            max_length=MAX_TELEMETRY_SCHEMA_ENTRY_LENGTH,
            field_name="telemetry_schema",
        )


class UiSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    components: list[UiComponent]
    spec_hash: str | None = None

    def validate(self, registry: Iterable[ComponentType | str]) -> "UiSpec":
        validate_generated_uispec(self, registry)
        return self

    def compute_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"spec_hash"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def finalized(self, registry: Iterable[ComponentType | str]) -> "UiSpec":
        self.validate(registry)
        return self.model_copy(update={"spec_hash": self.compute_hash()})


class InlineGenUiEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    envelope_id: str = Field(min_length=1, max_length=80)
    envelope_hash: str | None = Field(default=None, max_length=128)
    run_id: str = Field(min_length=1, max_length=80)
    run_manifest_hash: str = Field(min_length=1, max_length=128)
    manifest_signature_ok: bool
    active_module_set_hash: str = Field(min_length=1, max_length=128)
    candidate_hash: str | None = Field(default=None, max_length=80)
    canary_id: str | None = Field(default=None, max_length=80)
    ui_spec_hash: str | None = Field(default=None, max_length=128)
    components: list[UiComponent] = Field(default_factory=list, max_length=MAX_COMPONENTS)
    provenance: dict[str, str] = Field(default_factory=dict)
    redaction: dict[str, bool] = Field(default_factory=dict)
    created_at: float

    @field_validator("provenance")
    @classmethod
    def validate_provenance(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 12:
            raise ValueError("provenance has too many entries")
        for key, item in value.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 60:
                raise ValueError("provenance keys must be bounded strings")
            if not isinstance(item, str) or len(item) > 160:
                raise ValueError("provenance values must be bounded strings")
        return value

    @field_validator("redaction")
    @classmethod
    def validate_redaction(cls, value: dict[str, bool]) -> dict[str, bool]:
        if len(value) > 12:
            raise ValueError("redaction has too many entries")
        for key, item in value.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 60:
                raise ValueError("redaction keys must be bounded strings")
            if not isinstance(item, bool):
                raise ValueError("redaction values must be booleans")
        return value

    def validate(self, registry: Iterable[ComponentType | str]) -> "InlineGenUiEnvelope":
        validate_inline_envelope(self, registry)
        return self

    def compute_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"envelope_hash"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def finalized(self, registry: Iterable[ComponentType | str]) -> "InlineGenUiEnvelope":
        self.validate(registry)
        return self.model_copy(update={"envelope_hash": self.compute_hash()})


class ActionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    type: ActionType
    payload: dict[str, Any] = Field(default_factory=dict)
    csrf_token: str | None = None
    active_pointer_version: int | None = None


def validate_action(
    cmd: ActionCommand,
    *,
    session_authed: bool,
    csrf_ok: bool,
    current_pointer_version: int,
    policy_ok: bool,
) -> None:
    cmd = ActionCommand.model_validate(cmd)
    if cmd.type not in PRIVILEGED_ACTIONS:
        return
    if not session_authed:
        raise PermissionError("privileged action requires authenticated session")
    if not csrf_ok:
        raise PermissionError("privileged action requires a valid CSRF token")
    if cmd.active_pointer_version != current_pointer_version:
        raise PermissionError("privileged action rejected stale active pointer version")
    if not policy_ok:
        raise PermissionError("privileged action rejected by policy")


def validate_declared_actions(actions: Any) -> list[ActionType]:
    if actions is None:
        return []
    if not isinstance(actions, list):
        raise ValueError("component actions must be a list")
    if len(actions) > MAX_COMPONENT_ACTIONS:
        raise ValueError("component actions has too many entries")
    normalized: list[ActionType] = []
    for action in actions:
        action_type = _declared_action_type(action)
        if action_type in PRIVILEGED_ACTIONS:
            raise PermissionError("privileged actions rejected in generated UiSpec")
        normalized.append(action_type)
    return normalized



def validate_component_props(component: UiComponent) -> BaseModel:
    component = UiComponent.model_validate(component)
    props_model = COMPONENT_PROP_MODELS.get(component.type)
    if props_model is None:
        raise ValueError(f"No props schema registered for component type: {component.type.value}")
    return props_model.model_validate(component.props)


def validate_generated_uispec(uispec: UiSpec, registry: Iterable[ComponentType | str]) -> None:
    allowed = {_component_type(item) for item in registry}
    if len(uispec.components) > MAX_COMPONENTS:
        raise ValueError(f"UiSpec exceeds maximum component count: {MAX_COMPONENTS}")
    unknown = [component.type for component in uispec.components if component.type not in allowed]
    if unknown:
        names = ", ".join(sorted({item.value for item in unknown}))
        raise ValueError(f"UiSpec references unknown component type(s): {names}")
    for component in uispec.components:
        _validate_component_actions(component)
        validate_component_props(component)


def validate_inline_envelope(envelope: InlineGenUiEnvelope, registry: Iterable[ComponentType | str]) -> None:
    allowed = {_component_type(item) for item in registry}
    if len(envelope.components) > MAX_COMPONENTS:
        raise ValueError(f"InlineGenUiEnvelope exceeds maximum component count: {MAX_COMPONENTS}")
    unknown = [component.type for component in envelope.components if component.type not in allowed]
    if unknown:
        names = ", ".join(sorted({item.value for item in unknown}))
        raise ValueError(f"InlineGenUiEnvelope references unknown component type(s): {names}")
    for component in envelope.components:
        _validate_component_actions(component)
        validate_component_props(component)


def build_uispec_from_manifest(module_set_manifest: Any, ui_registry: Iterable[ComponentType | str]) -> UiSpec:
    allowed = {_component_type(item) for item in ui_registry}
    components: list[UiComponent] = []
    for index, panel in enumerate(getattr(module_set_manifest, "resolved_ui_panels", [])):
        component_type = _component_type(_panel_name(panel))
        if component_type not in allowed:
            continue
        components.append(
            UiComponent(
                type=component_type,
                region=_panel_region(component_type),
                priority=_panel_priority(panel, index),
                props={"panel": panel, "manifest_hash": getattr(module_set_manifest, "manifest_hash", None)},
                telemetry_schema=["rendered", "action", "latency_ms"],
            )
        )
    return UiSpec(components=sorted(components, key=lambda item: (item.priority, item.type.value))[:MAX_COMPONENTS]).finalized(allowed)


def _component_type(value: ComponentType | str) -> ComponentType:
    if isinstance(value, ComponentType):
        return value
    try:
        return ComponentType(value)
    except ValueError:
        return ComponentType[value]


def _panel_name(panel: str) -> str:
    return panel.split(":", 1)[0]


def _panel_priority(panel: str, fallback: int = 0) -> int:
    if ":" not in panel:
        return fallback
    try:
        return int(panel.rsplit(":", 1)[1])
    except ValueError:
        return fallback


def _panel_region(component_type: ComponentType) -> Region:
    if component_type in {ComponentType.APPROVAL_PANEL, ComponentType.ROLLBACK_PANEL, ComponentType.SAFETY_STATUS_CARD}:
        return Region.ACTIONS
    if component_type in {
        ComponentType.TRACE_PANEL,
        ComponentType.MUTATION_DIFF_PANEL,
        ComponentType.TOOL_RESULT_CARD,
        ComponentType.HARNESS_EVOLUTION_CARD,
        ComponentType.EVIDENCE_STATUS_CARD,
        ComponentType.TIMELINE_STEP,
    }:
        return Region.DETAILS
    if component_type in {ComponentType.INTAKE_PANEL, ComponentType.CONTEXT_PANEL, ComponentType.ORB_STATUS}:
        return Region.SIDEBAR
    return Region.MAIN


def _bounded_strings(value: list[str], *, max_items: int, max_length: int, field_name: str) -> list[str]:
    if len(value) > max_items:
        raise ValueError(f"{field_name} has too many entries")
    for item in value:
        if not isinstance(item, str) or not 1 <= len(item) <= max_length:
            raise ValueError(f"{field_name} entries must be bounded strings")
    return value


def _declared_action_type(action: Any) -> ActionType:
    raw_type: Any
    if isinstance(action, ActionType):
        raw_type = action.value
    elif isinstance(action, str):
        raw_type = action
    elif isinstance(action, dict) and "type" in action:
        raw_type = action["type"]
    else:
        raise ValueError("component action must be a string or typed object")
    try:
        return ActionType(raw_type)
    except ValueError as exc:
        raise ValueError(f"unknown component action type: {raw_type}") from exc


def _validate_component_actions(component: UiComponent) -> None:
    validate_declared_actions(component.props.get("actions", []))
