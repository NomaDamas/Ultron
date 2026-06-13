"""Core harness module identity model."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field
from ultron.module.blobs import BlobKind, BlobStore, BudgetPolicyBlob, PromptPack, SafetyPolicyBlob, ToolPolicyBlob, UiPanelContract


from ultron.hermes.capability import AdapterCapabilityContract, AttachSurface, CapabilityStatus
from ultron.hermes.module_surface_contract import ModuleSurfaceContract


class TargetLens(StrEnum):
    COMMUNITY = "COMMUNITY"
    DEVELOPER = "DEVELOPER"
    OPS = "OPS"
    PERSONAL = "PERSONAL"


class EvidenceLabel(StrEnum):
    PREFERENCE = "preference_evidence"
    BENCHMARK = "benchmark_evidence"
    CAUSAL_SUFFICIENT = "causal_sufficient_for_mvp"
    INSUFFICIENT = "insufficient_evidence"


class PromotionState(StrEnum):
    SEED = "SEED"
    CANDIDATE = "CANDIDATE"
    SURVIVOR = "SURVIVOR"
    DECAYING = "DECAYING"
    PRUNED = "PRUNED"
    QUARANTINED = "QUARANTINED"


class PersistencePolicy(StrEnum):
    READ_ONLY = "READ_ONLY"
    ISOLATED = "ISOLATED"
    CHECKPOINTED = "CHECKPOINTED"
    NORMAL = "NORMAL"


class FitnessMetadata(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    primary_metric: float | None = None
    guardrails: dict[str, float] = Field(default_factory=dict)
    usage_count: int = 0
    last_used_at: float | None = None
    decay_score: float = 0.0
    evidence_labels: list[EvidenceLabel] = Field(default_factory=list)
    promotion_state: PromotionState = PromotionState.SEED


class PrivacyMetadata(BaseModel):
    owner_scope: str
    consent_class: str = "operational"
    global_template_eligible: bool = False
    redaction_status: str = "none"
    retention_rule: str = "default"


class HarnessModule(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    module_id: str
    name: str
    version: int = Field(ge=1)
    parent_id: str | None = None
    workflow_tags: list[str] = Field(default_factory=list)
    target_lens: TargetLens
    owner_scope: str
    surfaces: ModuleSurfaceContract
    prompt_pack_hash: str | None = None
    tool_allowlist_hash: str | None = None
    skill_refs: list[str] = Field(default_factory=list)
    topology_fragment_hash: str | None = None
    ui_panel_contract_hash: str | None = None
    safety_policy_hash: str | None = None
    budget_policy_hash: str | None = None
    persistence_policy: PersistencePolicy = PersistencePolicy.ISOLATED
    required_adapter_capabilities: list[AttachSurface] = Field(default_factory=list)
    hermes_version_range: str
    privacy: PrivacyMetadata
    fitness: FitnessMetadata = Field(default_factory=FitnessMetadata)
    content_hash: str | None = None

    @classmethod
    def identity_fields(cls) -> tuple[str, ...]:
        return tuple(name for name in cls.model_fields if name not in {"content_hash", "fitness"})

    def _identity_payload(self) -> dict[str, Any]:
        identity = self.model_dump(
            mode="json",
            include=set(self.identity_fields()),
        )
        return dict(sorted(identity.items()))

    def compute_content_hash(self) -> str:
        """Return the stable identity hash, excluding content_hash and runtime fitness."""
        canonical = json.dumps(
            self._identity_payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def finalized(self) -> Self:
        """Return a copy with content_hash set to the deterministic identity hash."""
        return self.model_copy(update={"content_hash": self.compute_content_hash()})

    @classmethod
    def create(cls, **data: Any) -> Self:
        """Build and finalize a harness module in one step."""
        return cls.model_validate(data).finalized()

    @classmethod
    def create_with_blobs(
        cls,
        blob_store: BlobStore,
        *,
        prompt_pack: PromptPack,
        tools: ToolPolicyBlob,
        ui: UiPanelContract,
        safety: SafetyPolicyBlob,
        budget: BudgetPolicyBlob,
        **identity_fields: Any,
    ) -> Self:
        """Build a finalized module whose artifact hash fields reference stored blobs."""
        blob_hashes = {
            "prompt_pack_hash": blob_store.put(BlobKind.PROMPT_PACK, prompt_pack),
            "tool_allowlist_hash": blob_store.put(BlobKind.TOOL_POLICY, tools),
            "ui_panel_contract_hash": blob_store.put(BlobKind.UI_PANEL_CONTRACT, ui),
            "safety_policy_hash": blob_store.put(BlobKind.SAFETY_POLICY, safety),
            "budget_policy_hash": blob_store.put(BlobKind.BUDGET_POLICY, budget),
        }
        return cls.create(**identity_fields, **blob_hashes)

    def referenced_blob_hashes(self) -> dict[BlobKind, str | None]:
        """Return the artifact blob references carried by this module."""
        return {
            BlobKind.PROMPT_PACK: self.prompt_pack_hash,
            BlobKind.TOOL_POLICY: self.tool_allowlist_hash,
            BlobKind.UI_PANEL_CONTRACT: self.ui_panel_contract_hash,
            BlobKind.SAFETY_POLICY: self.safety_policy_hash,
            BlobKind.BUDGET_POLICY: self.budget_policy_hash,
        }

    def validate_surfaces(self, contract: AdapterCapabilityContract) -> None:
        """Validate declared and required attach surfaces against the adapter contract."""
        ModuleSurfaceContract.validated(self.surfaces.model_dump(), contract)
        deferred = [
            surface.value
            for surface in self.required_adapter_capabilities
            if contract.get(surface).status == CapabilityStatus.DEFERRED
        ]
        if deferred:
            detail = ", ".join(sorted(deferred))
            raise ValueError(f"required adapter capability is deferred: {detail}")
