import pytest

from ultron.app.triage import DEFAULT_SCOPE, DEFAULT_WORKFLOW, TriageApp
from ultron.hermes.module_surface_contract import ModuleSurfaceContract
from ultron.module.blobs import (
    BlobKind,
    BlobStore,
    BudgetPolicyBlob,
    PromptPack,
    SafetyPolicyBlob,
    ToolPolicyBlob,
    UiPanelContract,
)
from ultron.module.model import FitnessMetadata, HarnessModule, PersistencePolicy, PrivacyMetadata, PromotionState, TargetLens
from ultron.registry.store import ModuleLifecycle, ModuleRegistry


def _identity_fields(**overrides):
    data = {
        "module_id": "mod.blobbed",
        "name": "Blobbed",
        "version": 1,
        "workflow_tags": ["code-triage"],
        "target_lens": TargetLens.DEVELOPER,
        "owner_scope": "team-alpha",
        "surfaces": ModuleSurfaceContract(
            prompt_slots=["triage.plan"],
            tools=["read"],
            ui_panels=["PLAN_PANEL:10"],
            safety={"workspace_writes": False, "external_calls": False},
            budget={"max_tool_calls": 8},
            persistence={"mode": PersistencePolicy.ISOLATED.value},
        ),
        "persistence_policy": PersistencePolicy.ISOLATED,
        "hermes_version_range": "pinned",
        "privacy": PrivacyMetadata(owner_scope="team-alpha"),
        "fitness": FitnessMetadata(promotion_state=PromotionState.SEED, usage_count=1, primary_metric=1.0),
    }
    data.update(overrides)
    return data


def _blobs():
    return {
        "prompt_pack": PromptPack(slots={"triage.plan": "Plan the requested code change."}, notes="baseline"),
        "tools": ToolPolicyBlob(tools=["read"], rationale="inspection"),
        "ui": UiPanelContract(panels=["PLAN_PANEL:10"], notes="plan panel"),
        "safety": SafetyPolicyBlob(workspace_writes=False, external_calls=False, extra_rules={"network": "deny"}),
        "budget": BudgetPolicyBlob(max_tool_calls=8),
    }


def _blobbed_module(store: BlobStore, **overrides):
    return HarnessModule.create_with_blobs(store, **_blobs(), **_identity_fields(**overrides))


def test_blob_content_addressing_idempotent_and_deep_copy():
    first = PromptPack(slots={"b": "two", "a": "one"}, notes="same")
    second = PromptPack(slots={"a": "one", "b": "two"}, notes="same")
    different = PromptPack(slots={"a": "changed", "b": "two"}, notes="same")

    assert first.content_hash() == second.content_hash()
    assert first.content_hash() != different.content_hash()

    store = BlobStore()
    assert store.put(BlobKind.PROMPT_PACK, first) == first.content_hash()
    assert store.put(BlobKind.PROMPT_PACK, second) == first.content_hash()

    fetched = store.get(BlobKind.PROMPT_PACK, first.content_hash())
    assert isinstance(fetched, PromptPack)
    fetched.slots["a"] = "mutated"
    stored_again = store.get(BlobKind.PROMPT_PACK, first.content_hash())
    assert isinstance(stored_again, PromptPack)
    assert stored_again.slots["a"] == "one"


def test_blob_store_rejects_kind_type_mismatch():
    store = BlobStore()
    with pytest.raises(TypeError, match="PROMPT_PACK blob must be PromptPack"):
        store.put(BlobKind.PROMPT_PACK, ToolPolicyBlob(tools=["read"]))


def test_registry_verifies_missing_mismatched_and_complete_blobs():
    store = BlobStore()
    registry = ModuleRegistry(store)
    module = _blobbed_module(store)
    entry = registry.register(module, ModuleLifecycle.SEED, "tenant")
    assert entry.module.content_hash == module.content_hash

    missing_hash = "0" * 64
    missing = module.model_copy(update={"prompt_pack_hash": missing_hash}).finalized()
    with pytest.raises(ValueError, match="missing blob.*PROMPT_PACK"):
        registry.register(missing, ModuleLifecycle.SEED, "tenant")

    placeholder = module.model_copy(update={"prompt_pack_hash": "candidate-placeholder"}).finalized()
    with pytest.raises(ValueError, match="artifact ref not blob-backed.*PROMPT_PACK"):
        registry.register(placeholder, ModuleLifecycle.SEED, "tenant")

    tampered_hash = module.prompt_pack_hash or ""
    store._blobs[(BlobKind.PROMPT_PACK, tampered_hash)] = PromptPack(slots={"triage.plan": "tampered"})
    mismatched = module.model_copy(update={"content_hash": None}).finalized()
    with pytest.raises(ValueError, match="blob hash mismatch.*PROMPT_PACK"):
        ModuleRegistry(store).register(mismatched, ModuleLifecycle.SEED, "tenant")


def test_create_with_blobs_sets_hashes_and_stable_module_identity_excludes_fitness():
    first_store = BlobStore()
    first = _blobbed_module(first_store)
    blobs = _blobs()

    assert first.prompt_pack_hash == blobs["prompt_pack"].content_hash()
    assert first.tool_allowlist_hash == blobs["tools"].content_hash()
    assert first.ui_panel_contract_hash == blobs["ui"].content_hash()
    assert first.safety_policy_hash == blobs["safety"].content_hash()
    assert first.budget_policy_hash == blobs["budget"].content_hash()

    second = _blobbed_module(BlobStore())
    assert second.content_hash == first.content_hash

    changed_fitness = first.model_copy(update={"fitness": FitnessMetadata(promotion_state=PromotionState.CANDIDATE, usage_count=99)})
    assert changed_fitness.compute_content_hash() == first.content_hash


def test_seed_baseline_is_blob_backed_and_start_run_end_to_end():
    app = TriageApp()
    baseline = app.seed_baseline()

    assert baseline.prompt_pack_hash and "baseline-" not in baseline.prompt_pack_hash
    assert baseline.tool_allowlist_hash and "baseline-" not in baseline.tool_allowlist_hash
    assert baseline.ui_panel_contract_hash and "baseline-" not in baseline.ui_panel_contract_hash
    assert baseline.safety_policy_hash and "baseline-" not in baseline.safety_policy_hash
    assert baseline.budget_policy_hash and "baseline-" not in baseline.budget_policy_hash

    for kind, content_hash in baseline.referenced_blob_hashes().items():
        assert content_hash is not None
        assert app.blob_store.has(kind, content_hash)
        assert app.blob_store.get(kind, content_hash).content_hash() == content_hash

    result = app.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "Fix a flaky test")
    assert result["run_manifest"].verify(signer=app.manifest_signer)
    assert result["adapter_result"].output


def test_prompt_slot_edit_canary_creates_real_prompt_pack_blob_and_registers_strictly():
    app = TriageApp()
    baseline = app.seed_baseline()

    canary = app.propose_and_canary("PROMPT_SLOT_EDIT", {"prompt_pack_hash": "Rewrite triage plan slot."})
    candidate = canary["candidate"]

    assert candidate.prompt_pack_hash != baseline.prompt_pack_hash
    assert candidate.prompt_pack_hash is not None
    stored = app.blob_store.get(BlobKind.PROMPT_PACK, candidate.prompt_pack_hash)
    assert isinstance(stored, PromptPack)
    assert stored.content_hash() == candidate.prompt_pack_hash
    assert "Rewrite triage plan slot." in stored.slots.values()
    assert app.registry.get(candidate.content_hash or "").module.prompt_pack_hash == candidate.prompt_pack_hash
