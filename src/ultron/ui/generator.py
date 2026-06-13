"""Fail-closed UI generation seams."""

from __future__ import annotations

from typing import Any, Iterable, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ultron.ui.runtime import ActionType, ComponentType, PRIVILEGED_ACTIONS, UiSpec, build_uispec_from_manifest


class LiveModelUnavailable(RuntimeError):
    """Raised when a live model seam is selected without configured model access."""


class UiGenContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    module_set_manifest: Any
    request_class: str
    run_output_summary: dict[str, Any] = Field(default_factory=dict)
    allowed_registry: list[ComponentType] = Field(default_factory=list)


class UiSpecGenerator(Protocol):
    @property
    def is_live(self) -> bool: ...

    @property
    def provider_id(self) -> str: ...

    def generate(self, context: UiGenContext) -> UiSpec: ...


class DeterministicFakeUiSpecGenerator:
    @property
    def is_live(self) -> bool:
        return False

    @property
    def provider_id(self) -> str:
        return "deterministic-fake-ui-generator"

    def generate(self, context: UiGenContext) -> UiSpec:
        spec = build_uispec_from_manifest(context.module_set_manifest, context.allowed_registry)
        return validate_generated_uispec(spec, context.allowed_registry)


class LiveModelUiSpecGenerator:
    @property
    def is_live(self) -> bool:
        return True

    @property
    def provider_id(self) -> str:
        return "live-model-ui-generator"

    def build_prompt(self, context: UiGenContext) -> dict[str, Any]:
        return {
            "request_class": context.request_class,
            "run_output_summary": context.run_output_summary,
            "resolved_ui_panels": list(getattr(context.module_set_manifest, "resolved_ui_panels", [])),
            "allowed_components": [item.value for item in context.allowed_registry],
            "security": "Emit only server-owned component types and non-privileged declared actions.",
        }

    def generate(self, context: UiGenContext) -> UiSpec:
        self.build_prompt(context)
        raise LiveModelUnavailable("live model UI generation requires a configured model")


def validate_generated_uispec(spec: UiSpec | dict[str, Any], registry: Iterable[ComponentType | str]) -> UiSpec:
    parsed = UiSpec.model_validate(spec).finalized(registry)
    for component in parsed.components:
        actions = component.props.get("actions", [])
        if actions is None:
            continue
        if not isinstance(actions, list):
            raise ValueError("generated UiSpec actions must be a list")
        for action in actions:
            action_type = _action_type(action)
            if action_type in PRIVILEGED_ACTIONS:
                raise PermissionError("generated UiSpec cannot define privileged actions")
    return parsed


def _action_type(action: Any) -> ActionType:
    if isinstance(action, ActionType):
        return action
    if isinstance(action, str):
        return ActionType(action)
    if isinstance(action, dict) and "type" in action:
        return ActionType(action["type"])
    raise ValueError("generated UiSpec action must be typed")
