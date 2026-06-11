"""Adapter capability contract for safe Hermes attach surfaces."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CapabilityStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    DEFERRED = "DEFERRED"
    ISOLATED_HOME_FALLBACK = "ISOLATED_HOME_FALLBACK"


class AttachSurface(StrEnum):
    SESSION_START = "session-start"
    PROMPT_SLOT_INJECTION = "prompt-slot-injection"
    TOOL_TOOLSET_ALLOWLIST = "tool-toolset-allowlist"
    SKILL_REFERENCE = "skill-reference"
    RUN_TAGGING = "run-tagging"
    TRACE_EXPORT = "trace-export"
    BUDGET_ENFORCEMENT = "budget-enforcement"
    MEMORY_SKILL_ISOLATION = "memory-skill-isolation"
    OUTCOME_EXPORT = "outcome-export"
    TOPOLOGY_SUBAGENT_CONTROL = "topology-subagent-control"
    CRON_GATEWAY_MCP_MUTATION = "cron-gateway-mcp-mutation"

    @classmethod
    def required_surfaces(cls) -> set[Self]:
        return set(cls)


class CapabilitySpec(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    surface: AttachSurface
    status: CapabilityStatus
    hermes_refs: list[str] = Field(default_factory=list)
    rule: str
    fallback: str | None = None

    @field_validator("hermes_refs")
    @classmethod
    def refs_must_be_strings(cls, refs: list[str]) -> list[str]:
        if not all(isinstance(ref, str) and ref for ref in refs):
            raise ValueError("hermes_refs must contain non-empty strings")
        return refs


class AdapterCapabilityContract(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    hermes_commit: str
    surfaces: list[CapabilitySpec]

    @classmethod
    def from_yaml(cls, path: str | Path) -> Self:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        contract = cls.model_validate(data)
        contract.validate()
        return contract

    def to_yaml(self) -> str:
        data = self.model_dump(mode="json")
        return yaml.safe_dump(data, sort_keys=False)

    @model_validator(mode="after")
    def _validate_model(self) -> Self:
        self.validate()
        return self

    def validate(self) -> None:
        seen: set[AttachSurface] = set()
        duplicates: list[str] = []
        for spec in self.surfaces:
            if spec.surface in seen:
                duplicates.append(spec.surface.value)
            seen.add(spec.surface)
        missing = AttachSurface.required_surfaces() - seen
        if missing:
            missing_values = ", ".join(sorted(surface.value for surface in missing))
            raise ValueError(f"missing attach surfaces: {missing_values}")
        if duplicates:
            duplicate_values = ", ".join(sorted(duplicates))
            raise ValueError(f"duplicate attach surfaces: {duplicate_values}")

    def get(self, surface: AttachSurface | str) -> CapabilitySpec:
        attach_surface = AttachSurface(surface)
        for spec in self.surfaces:
            if spec.surface == attach_surface:
                return spec
        raise KeyError(f"unknown attach surface: {attach_surface.value}")

    def is_supported(self, surface: AttachSurface | str) -> bool:
        return self.get(surface).status == CapabilityStatus.SUPPORTED

    def require(self, surface: AttachSurface | str) -> CapabilitySpec:
        spec = self.get(surface)
        if spec.status == CapabilityStatus.DEFERRED:
            raise ValueError(f"attach surface is deferred: {spec.surface.value}")
        return spec
