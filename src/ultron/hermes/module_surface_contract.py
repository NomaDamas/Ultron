"""Module-facing surface contract and preserved-core prohibitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from ultron.hermes.capability import AdapterCapabilityContract, AttachSurface, CapabilityStatus


PROMPT_SLOT_SURFACE = AttachSurface.PROMPT_SLOT_INJECTION
MODULE_SURFACE_MAP: dict[str, AttachSurface] = {
    "prompt_slots": AttachSurface.PROMPT_SLOT_INJECTION,
    "tools": AttachSurface.TOOL_TOOLSET_ALLOWLIST,
    "skill_refs": AttachSurface.SKILL_REFERENCE,
    "topology_fragment": AttachSurface.TOPOLOGY_SUBAGENT_CONTROL,
    "ui_panels": AttachSurface.OUTCOME_EXPORT,
    "safety": AttachSurface.BUDGET_ENFORCEMENT,
    "budget": AttachSurface.BUDGET_ENFORCEMENT,
    "persistence": AttachSurface.MEMORY_SKILL_ISOLATION,
}

PRESERVED_CORE_PROHIBITIONS: tuple[str, ...] = (
    "hermes_source_mutation",
    "global_memory_write",
    "global_skill_write",
    "tool_impl_mutation",
    "backend_mutation",
    "cron_gateway_mcp_mutation",
    "credential_mutation",
)


class ModuleSurfaceContract(BaseModel):
    """Attach surfaces a module may declare."""

    prompt_slots: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    skill_refs: list[str] = Field(default_factory=list)
    topology_fragment: dict[str, Any] | None = None
    ui_panels: list[str] = Field(default_factory=list)
    safety: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    persistence: dict[str, Any] | None = None


@dataclass(frozen=True)
class Violation:
    surface: str
    reason: str


def _declared(value: Any) -> bool:
    return value is not None and value != [] and value != {} and value is not False


def validate_module_surfaces(
    declared: dict[str, Any], contract: AdapterCapabilityContract
) -> list[Violation]:
    """Return violations for declared prohibited or deferred module surfaces."""
    violations: list[Violation] = []

    for key in declared:
        if key in PRESERVED_CORE_PROHIBITIONS:
            violations.append(Violation(key, "preserved-core prohibition"))

    for key, attach_surface in MODULE_SURFACE_MAP.items():
        if not _declared(declared.get(key)):
            continue
        spec = contract.get(attach_surface)
        if spec.status == CapabilityStatus.DEFERRED:
            violations.append(
                Violation(key, f"attach surface is deferred: {attach_surface.value}")
            )

    if _declared(declared.get("cron_gateway_mcp_mutation")):
        spec = contract.get(AttachSurface.CRON_GATEWAY_MCP_MUTATION)
        violations.append(
            Violation(
                "cron_gateway_mcp_mutation",
                f"preserved-core prohibition; attach surface is {spec.status.value}",
            )
        )

    return violations
