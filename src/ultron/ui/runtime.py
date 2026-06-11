"""Server-owned generative UI runtime and typed action validation."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field


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


class ActionType(StrEnum):
    SUBMIT_REQUEST = "SUBMIT_REQUEST"
    GIVE_FEEDBACK = "GIVE_FEEDBACK"
    APPROVE_PROMOTION = "APPROVE_PROMOTION"
    ROLLBACK_CANARY = "ROLLBACK_CANARY"
    RESTORE_MODULE = "RESTORE_MODULE"
    REQUEST_PERMISSION_EXPANSION = "REQUEST_PERMISSION_EXPANSION"


PRIVILEGED_ACTIONS: set[ActionType] = {
    ActionType.APPROVE_PROMOTION,
    ActionType.ROLLBACK_CANARY,
    ActionType.RESTORE_MODULE,
    ActionType.REQUEST_PERMISSION_EXPANSION,
}


class UiComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    type: ComponentType
    region: str
    priority: int
    props: dict[str, Any] = Field(default_factory=dict)
    telemetry_schema: list[str] = Field(default_factory=list)


class UiSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    components: list[UiComponent]
    spec_hash: str | None = None

    def validate(self, registry: Iterable[ComponentType | str]) -> "UiSpec":
        allowed = {_component_type(item) for item in registry}
        unknown = [component.type for component in self.components if component.type not in allowed]
        if unknown:
            names = ", ".join(sorted({item.value for item in unknown}))
            raise ValueError(f"UiSpec references unknown component type(s): {names}")
        return self

    def compute_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"spec_hash"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def finalized(self, registry: Iterable[ComponentType | str]) -> "UiSpec":
        self.validate(registry)
        return self.model_copy(update={"spec_hash": self.compute_hash()})


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
    return UiSpec(components=sorted(components, key=lambda item: (item.priority, item.type.value))).finalized(allowed)


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


def _panel_region(component_type: ComponentType) -> str:
    if component_type in {ComponentType.APPROVAL_PANEL, ComponentType.ROLLBACK_PANEL}:
        return "actions"
    if component_type in {ComponentType.TRACE_PANEL, ComponentType.MUTATION_DIFF_PANEL}:
        return "details"
    if component_type in {ComponentType.INTAKE_PANEL, ComponentType.CONTEXT_PANEL}:
        return "sidebar"
    return "main"
