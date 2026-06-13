"""Fail-closed module synthesis seams."""

from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ultron.hermes.capability import AdapterCapabilityContract
from ultron.module.contract import load_default_contract
from ultron.hermes.module_surface_contract import ModuleSurfaceContract
from ultron.module.blobs import BlobStore, BudgetPolicyBlob, PromptPack, SafetyPolicyBlob, ToolPolicyBlob, UiPanelContract
from ultron.module.model import FitnessMetadata, HarnessModule, PersistencePolicy, PrivacyMetadata, PromotionState, TargetLens
from ultron.registry.store import ModuleRegistry
from ultron.ui.generator import LiveModelUnavailable, ModelProvider


class SynthesisPolicyConstraints(BaseModel):
    allowed_surfaces: ModuleSurfaceContract
    no_permission_expansion: bool = True


class SynthesisContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    request_text: str
    workflow_fingerprint: str
    parent_module: HarnessModule | None = None
    feedback_summary: Any | None = None
    eval_summary: dict[str, Any] | None = None
    policy_constraints: SynthesisPolicyConstraints


class ModuleSynthesizer(Protocol):
    @property
    def is_live(self) -> bool: ...

    @property
    def provider_id(self) -> str: ...

    def synthesize(self, context: SynthesisContext) -> HarnessModule: ...


class DeterministicFakeModuleSynthesizer:
    def __init__(self, blob_store: BlobStore, adapter_contract: AdapterCapabilityContract) -> None:
        self.blob_store = blob_store
        self.adapter_contract = adapter_contract

    @property
    def is_live(self) -> bool:
        return False

    @property
    def provider_id(self) -> str:
        return "deterministic-fake-module-synthesizer"

    def synthesize(self, context: SynthesisContext) -> HarnessModule:
        parent = context.parent_module
        surfaces = _bounded_surfaces(parent, context.policy_constraints.allowed_surfaces)
        prompt_slots = surfaces.prompt_slots or ["synthesis.request"]
        prompt_pack = PromptPack(
            slots={slot: _prompt_text(context, slot) for slot in prompt_slots},
            notes=f"Deterministic synthesis for {context.workflow_fingerprint}.",
        )
        tools = ToolPolicyBlob(tools=list(surfaces.tools), rationale="Bounded to parent and allowed surfaces; no permission expansion.")
        ui = UiPanelContract(panels=list(surfaces.ui_panels), notes="Inherited bounded UI panels.")
        safety = SafetyPolicyBlob(
            workspace_writes=bool((surfaces.safety or {}).get("workspace_writes", False)),
            external_calls=bool((surfaces.safety or {}).get("external_calls", False)),
            extra_rules={k: v for k, v in (surfaces.safety or {}).items() if k not in {"workspace_writes", "external_calls"}},
        )
        budget = BudgetPolicyBlob(max_tool_calls=int((surfaces.budget or {}).get("max_tool_calls", 1)))
        module = HarnessModule.create_with_blobs(
            self.blob_store,
            module_id=_module_id(context),
            name=f"Synthesized {context.workflow_fingerprint}",
            version=(parent.version + 1 if parent else 1),
            parent_id=parent.content_hash if parent else None,
            workflow_tags=[context.workflow_fingerprint],
            target_lens=parent.target_lens if parent else TargetLens.DEVELOPER,
            owner_scope=parent.owner_scope if parent else "default-user",
            surfaces=surfaces,
            prompt_pack=prompt_pack,
            tools=tools,
            ui=ui,
            safety=safety,
            budget=budget,
            persistence_policy=parent.persistence_policy if parent else PersistencePolicy.ISOLATED,
            hermes_version_range=parent.hermes_version_range if parent else "pinned",
            privacy=parent.privacy if parent else PrivacyMetadata(owner_scope="default-user", data_classes=["operational"], consent_basis="synthesis"),
            fitness=FitnessMetadata(promotion_state=PromotionState.CANDIDATE),
        )
        return validate_synthesized_module(module, self.adapter_contract, parent=parent, registry=None)


class LiveModelModuleSynthesizer:
    def __init__(self, provider: ModelProvider | None = None) -> None:
        self.provider = provider

    @property
    def is_live(self) -> bool:
        return True

    @property
    def provider_id(self) -> str:
        return "live-model-module-synthesizer"

    def build_prompt(self, context: SynthesisContext) -> dict[str, Any]:
        return {
            "request_text": context.request_text,
            "workflow_fingerprint": context.workflow_fingerprint,
            "parent_hash": context.parent_module.content_hash if context.parent_module else None,
            "allowed_surfaces": context.policy_constraints.allowed_surfaces.model_dump(mode="json"),
            "security": "Return a draft that does not expand permissions and can pass server-side contract validation.",
        }

    def synthesize(self, context: SynthesisContext) -> HarnessModule:
        if self.provider is None:
            raise LiveModelUnavailable("live model module synthesis requires a configured model")
        prompt = json.dumps(self.build_prompt(context), sort_keys=True)
        text = self.provider.complete(prompt, "HarnessModule JSON matching the server schema; no permission expansion; content_hash must match identity")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("live model module synthesis returned invalid JSON") from exc
        module = HarnessModule.model_validate(payload)
        return validate_synthesized_module(module, load_default_contract(), parent=context.parent_module, registry=None)


def validate_synthesized_module(
    module: HarnessModule,
    adapter_contract: AdapterCapabilityContract,
    *,
    parent: HarnessModule | None,
    registry: ModuleRegistry | None,
) -> HarnessModule:
    declared_hash = module.content_hash
    recomputed_hash = module.compute_content_hash()
    if declared_hash is not None and declared_hash != recomputed_hash:
        raise ValueError("synthesized module content hash mismatch")
    candidate = module.finalized()
    ModuleSurfaceContract.validated(candidate.surfaces.model_dump(), adapter_contract)
    candidate.validate_surfaces(adapter_contract)
    if parent is not None and _expands_permissions(candidate, parent):
        raise PermissionError("synthesized module requires human approval for permission expansion")
    if registry is not None and parent is not None and not registry.can_auto_promote(candidate):
        raise PermissionError("synthesized module is not auto-promotable")
    return candidate


def _bounded_surfaces(parent: HarnessModule | None, allowed: ModuleSurfaceContract) -> ModuleSurfaceContract:
    if parent is None:
        return ModuleSurfaceContract.model_validate(allowed.model_dump())
    return ModuleSurfaceContract(
        prompt_slots=sorted(set(parent.surfaces.prompt_slots) & set(allowed.prompt_slots)),
        tools=sorted(set(parent.surfaces.tools) & set(allowed.tools)),
        skill_refs=sorted(set(parent.surfaces.skill_refs) & set(allowed.skill_refs)),
        topology_fragment=None,
        ui_panels=sorted(set(parent.surfaces.ui_panels) & set(allowed.ui_panels)),
        safety=_bounded_safety(parent.surfaces.safety, allowed.safety),
        budget=_bounded_budget(parent.surfaces.budget, allowed.budget),
        persistence=parent.surfaces.persistence,
    )


def _bounded_safety(parent: dict[str, Any] | None, allowed: dict[str, Any] | None) -> dict[str, Any]:
    parent = parent or {}
    allowed = allowed or {}
    return {
        "workspace_writes": bool(parent.get("workspace_writes", False) and allowed.get("workspace_writes", False)),
        "external_calls": bool(parent.get("external_calls", False) and allowed.get("external_calls", False)),
    }


def _bounded_budget(parent: dict[str, Any] | None, allowed: dict[str, Any] | None) -> dict[str, Any]:
    parent_max = int((parent or {}).get("max_tool_calls", 1))
    allowed_max = int((allowed or {}).get("max_tool_calls", parent_max))
    return {"max_tool_calls": max(1, min(parent_max, allowed_max))}


def _expands_permissions(candidate: HarnessModule, parent: HarnessModule) -> bool:
    if not set(candidate.surfaces.tools).issubset(set(parent.surfaces.tools)):
        return True
    if not set(candidate.surfaces.skill_refs).issubset(set(parent.surfaces.skill_refs)):
        return True
    if candidate.surfaces.topology_fragment and candidate.surfaces.topology_fragment != parent.surfaces.topology_fragment:
        return True
    return _persistence_rank(candidate.persistence_policy) > _persistence_rank(parent.persistence_policy)


def _persistence_rank(policy: PersistencePolicy) -> int:
    return {PersistencePolicy.READ_ONLY: 0, PersistencePolicy.ISOLATED: 1, PersistencePolicy.NORMAL: 2}[policy]


def _prompt_text(context: SynthesisContext, slot: str) -> str:
    parent = f" Parent={context.parent_module.content_hash}." if context.parent_module and context.parent_module.content_hash else ""
    feedback = ""
    if context.feedback_summary is not None:
        feedback = f" Feedback={context.feedback_summary.model_dump(mode='json')}."
    return f"{slot}: {context.request_text.strip()} workflow={context.workflow_fingerprint}.{parent}{feedback}".strip()


def _module_id(context: SynthesisContext) -> str:
    base = context.parent_module.module_id if context.parent_module else context.workflow_fingerprint
    return f"{base}_synth"
