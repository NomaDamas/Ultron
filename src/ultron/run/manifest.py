"""Tamper-evident run manifest for resolved effective state."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from ultron.composition.manifest import ModuleSetManifest
from ultron.module.model import PersistencePolicy
from ultron.run.signer import ManifestSigner

DEFAULT_RUN_MANIFEST_SIGNING_KEY = "ultron-dev-run-manifest-key"


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def hmac_sha256(key: str, payload: str) -> str:
    return hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _policy_hash(policy: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(policy).encode("utf-8")).hexdigest()


class RunManifest(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    run_id: str
    session_id: str
    user_scope: str
    workflow_fingerprint: str
    active_module_set_id: str
    active_module_set_hash: str
    ordered_module_hashes: list[str]
    candidate_module_id: str | None = None
    variation_primitive_id: str | None = None
    canary_id: str | None = None
    hermes_version: str
    adapter_version: str
    contract_version: str
    model_snapshot: dict[str, Any]
    resolved_prompt_order: list[str]
    resolved_tool_allowlist: list[str]
    resolved_skill_refs: list[str]
    resolved_topology_hash: str | None = None
    resolved_ui_spec_hash: str | None = None
    safety_policy_hash: str
    budget_policy_hash: str
    persistence_mode: PersistencePolicy
    workspace_snapshot_id: str | None = None
    external_call_policy_id: str | None = None
    side_effect_ledger_id: str
    created_at: float
    timestamp_source: str
    key_id: str | None = None
    signature: str | None = None

    def canonical_payload(self) -> dict[str, Any]:
        """Return all signed fields, excluding only the signature itself."""
        return self.model_dump(mode="json", exclude={"signature"})

    def sign(self, key: str = DEFAULT_RUN_MANIFEST_SIGNING_KEY, *, signer: ManifestSigner | None = None, key_id: str | None = None) -> Self:
        payload = self.model_copy(update={"key_id": key_id or (signer.key_id if signer is not None else self.key_id)}).canonical_payload()
        signature = signer.sign(payload) if signer is not None else hmac_sha256(key, canonical_json(payload))
        return self.model_copy(update={"signature": signature, "key_id": payload.get("key_id")})

    def verify(self, key: str = DEFAULT_RUN_MANIFEST_SIGNING_KEY, *, signer: ManifestSigner | None = None, key_id: str | None = None) -> bool:
        if self.signature is None:
            return False
        expected_key_id = key_id or self.key_id
        payload = self.model_copy(update={"key_id": expected_key_id}).canonical_payload()
        if signer is not None:
            return signer.verify(payload, self.signature, expected_key_id or "")
        expected = hmac_sha256(key, canonical_json(payload))
        return hmac.compare_digest(self.signature, expected)

    @classmethod
    def from_manifest_set(
        cls,
        manifest: ModuleSetManifest,
        *,
        run_id: str,
        session_id: str,
        active_module_set_id: str,
        hermes_version: str,
        adapter_version: str,
        contract_version: str,
        model_snapshot: dict[str, Any],
        side_effect_ledger_id: str,
        created_at: float,
        timestamp_source: str,
        persistence_mode: PersistencePolicy,
        candidate_module_id: str | None = None,
        variation_primitive_id: str | None = None,
        canary_id: str | None = None,
        resolved_skill_refs: list[str] | None = None,
        resolved_topology_hash: str | None = None,
        resolved_ui_spec_hash: str | None = None,
        workspace_snapshot_id: str | None = None,
        external_call_policy_id: str | None = None,
        safety_policy_hash: str | None = None,
        budget_policy_hash: str | None = None,
    ) -> Self:
        manifest_hash = manifest.manifest_hash or manifest.compute_manifest_hash()
        return cls(
            run_id=run_id,
            session_id=session_id,
            user_scope=manifest.user_scope,
            workflow_fingerprint=manifest.workflow_fingerprint,
            active_module_set_id=active_module_set_id,
            active_module_set_hash=manifest_hash,
            ordered_module_hashes=list(manifest.ordered_module_hashes),
            candidate_module_id=candidate_module_id,
            variation_primitive_id=variation_primitive_id,
            canary_id=canary_id,
            hermes_version=hermes_version,
            adapter_version=adapter_version,
            contract_version=contract_version,
            model_snapshot=dict(model_snapshot),
            resolved_prompt_order=list(manifest.resolved_prompt_order),
            resolved_tool_allowlist=list(manifest.resolved_tool_allowlist),
            resolved_skill_refs=list(resolved_skill_refs if resolved_skill_refs is not None else manifest.resolved_skill_refs),
            resolved_topology_hash=resolved_topology_hash,
            resolved_ui_spec_hash=resolved_ui_spec_hash,
            safety_policy_hash=safety_policy_hash or _policy_hash(manifest.safety_policy),
            budget_policy_hash=budget_policy_hash or _policy_hash(manifest.budget_policy),
            persistence_mode=persistence_mode,
            workspace_snapshot_id=workspace_snapshot_id,
            external_call_policy_id=external_call_policy_id,
            side_effect_ledger_id=side_effect_ledger_id,
            created_at=created_at,
            timestamp_source=timestamp_source,
        )
