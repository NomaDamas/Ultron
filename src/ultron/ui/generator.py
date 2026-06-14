"""Fail-closed UI generation seams."""

from __future__ import annotations

import json
from typing import Any, Iterable, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ultron.ui.runtime import ComponentType, UiSpec, build_uispec_from_manifest


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


class ModelProvider(Protocol):
    def complete(self, prompt: str, schema_hint: str | None) -> str: ...


class LiveModelUiSpecGenerator:
    def __init__(self, provider: ModelProvider | None = None) -> None:
        self.provider = provider

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
        if self.provider is None:
            raise LiveModelUnavailable("live model UI generation requires a configured model")
        prompt = json.dumps(self.build_prompt(context), sort_keys=True)
        text = self.provider.complete(
            prompt,
            "UiSpec JSON with components[{type,region,priority,props,telemetry_schema(max 8 strings, max 80 chars each)}] and optional spec_hash",
        )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("live model UI generation returned invalid JSON") from exc
        return validate_generated_uispec(payload, context.allowed_registry)


def validate_generated_uispec(spec: UiSpec | dict[str, Any], registry: Iterable[ComponentType | str]) -> UiSpec:
    parsed = UiSpec.model_validate(spec)
    return parsed.finalized(registry)

