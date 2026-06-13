import pytest

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
from ultron.module.model import (
    FitnessMetadata,
    HarnessModule,
    PersistencePolicy,
    PrivacyMetadata,
    PromotionState,
    TargetLens,
)
from ultron.registry.store import ModuleLifecycle, ModuleRegistry


SHA_MISSING = "0" * 64
SHA_FORGED = "f" * 64


def _identity_fields(**overrides):
    data = {
        "module_id": "mod.gap2.redteam",
        "name": "GAP2 Redteam",
        "version": 1,
        "workflow_tags": ["redteam", "gap2"],
        "target_lens": TargetLens.DEVELOPER,
        "owner_scope": "qa-team",
        "surfaces": ModuleSurfaceContract(
            prompt_slots=["triage.plan"],
            tools=["read", "write"],
            ui_panels=["PLAN_PANEL:10"],
            safety={"workspace_writes": False, "external_calls": False},
            budget={"max_tool_calls": 8},
            persistence={"mode": PersistencePolicy.ISOLATED.value},
        ),
        "persistence_policy": PersistencePolicy.ISOLATED,
        "hermes_version_range": "pinned",
        "privacy": PrivacyMetadata(owner_scope="qa-team"),
        "fitness": FitnessMetadata(
            promotion_state=PromotionState.SEED,
            usage_count=1,
            primary_metric=0.5,
        ),
    }
    data.update(overrides)
    return data


def _blobs(**overrides):
    data = {
        "prompt_pack": PromptPack(
            slots={"triage.plan": "Plan the requested code change.", "triage.review": "Review it."},
            notes="baseline",
        ),
        "tools": ToolPolicyBlob(tools=["read", "write"], rationale="inspection and edits"),
        "ui": UiPanelContract(panels=["PLAN_PANEL:10"], notes="plan panel"),
        "safety": SafetyPolicyBlob(
            workspace_writes=False,
            external_calls=False,
            extra_rules={"network": "deny", "shell": "restricted"},
        ),
        "budget": BudgetPolicyBlob(max_tool_calls=8, max_cost=1.5, max_latency_s=30),
    }
    data.update(overrides)
    return data


def _blobbed_module(store: BlobStore, **overrides):
    return HarnessModule.create_with_blobs(store, **_blobs(), **_identity_fields(**overrides))


def test_content_addressing_is_canonical_idempotent_and_deep_copy():
    first = PromptPack(slots={"b": "two", "a": "one"}, notes="same")
    second = PromptPack.model_validate({"notes": "same", "slots": {"a": "one", "b": "two"}})
    changed = PromptPack(slots={"a": "one", "b": "changed"}, notes="same")

    assert first.content_hash() == second.content_hash()
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.content_hash() != changed.content_hash()

    store = BlobStore()
    first_id = store.put(BlobKind.PROMPT_PACK, first)
    second_id = store.put(BlobKind.PROMPT_PACK, second)

    assert first_id == second_id == first.content_hash()
    assert len(store._blobs) == 1

    fetched = store.get(BlobKind.PROMPT_PACK, first_id)
    assert isinstance(fetched, PromptPack)
    fetched.slots["a"] = "mutated"
    fetched.notes = "mutated"

    refetched = store.get(BlobKind.PROMPT_PACK, first_id)
    assert isinstance(refetched, PromptPack)
    assert refetched.slots == {"a": "one", "b": "two"}
    assert refetched.notes == "same"


def test_registry_rejects_sha_looking_missing_blob_reference():
    store = BlobStore()
    module = _blobbed_module(store)
    forged_missing = module.model_copy(update={"prompt_pack_hash": SHA_MISSING, "content_hash": None}).finalized()

    with pytest.raises(ValueError, match="missing blob.*PROMPT_PACK"):
        ModuleRegistry(store).register(forged_missing, ModuleLifecycle.SEED, "tenant")


def test_registry_rejects_mismatched_store_entry_and_forged_blob_reference():
    store = BlobStore()
    module = _blobbed_module(store)
    prompt_hash = module.prompt_pack_hash
    assert prompt_hash is not None

    store._blobs[(BlobKind.PROMPT_PACK, prompt_hash)] = PromptPack(slots={"triage.plan": "forged content B"})
    forged_content = module.model_copy(update={"content_hash": None}).finalized()
    with pytest.raises(ValueError, match="blob hash mismatch.*PROMPT_PACK"):
        ModuleRegistry(store).register(forged_content, ModuleLifecycle.SEED, "tenant")

    clean_store = BlobStore()
    real_blob = PromptPack(slots={"triage.plan": "content A"}, notes="real")
    real_hash = clean_store.put(BlobKind.PROMPT_PACK, real_blob)
    different_sha_reference = HarnessModule.create(
        **_identity_fields(module_id="mod.gap2.redteam.different-sha"),
        prompt_pack_hash=SHA_FORGED,
        tool_allowlist_hash=None,
        ui_panel_contract_hash=None,
        safety_policy_hash=None,
        budget_policy_hash=None,
    )
    assert real_hash != SHA_FORGED
    with pytest.raises(ValueError, match="missing blob.*PROMPT_PACK"):
        ModuleRegistry(clean_store).register(different_sha_reference, ModuleLifecycle.SEED, "tenant")


def test_create_with_blobs_identity_tracks_blob_hashes_but_excludes_fitness():
    first_store = BlobStore()
    first = _blobbed_module(first_store)
    second = _blobbed_module(BlobStore())

    assert second.prompt_pack_hash == first.prompt_pack_hash
    assert second.tool_allowlist_hash == first.tool_allowlist_hash
    assert second.ui_panel_contract_hash == first.ui_panel_contract_hash
    assert second.safety_policy_hash == first.safety_policy_hash
    assert second.budget_policy_hash == first.budget_policy_hash
    assert second.content_hash == first.content_hash

    changed_prompt = PromptPack(slots={"triage.plan": "Changed prompt."}, notes="baseline")
    changed = HarnessModule.create_with_blobs(
        BlobStore(),
        **_blobs(prompt_pack=changed_prompt),
        **_identity_fields(),
    )
    assert changed.prompt_pack_hash == changed_prompt.content_hash()
    assert changed.prompt_pack_hash != first.prompt_pack_hash
    assert changed.content_hash != first.content_hash

    changed_fitness = first.model_copy(
        update={
            "fitness": FitnessMetadata(
                promotion_state=PromotionState.SURVIVOR,
                usage_count=999,
                primary_metric=99.0,
            )
        }
    )
    assert changed_fitness.compute_content_hash() == first.content_hash
    assert changed_fitness.finalized().content_hash == first.content_hash


def test_legacy_placeholder_boundary_accepts_only_without_blob_store():
    store = BlobStore()
    legacy = HarnessModule.create(
        **_identity_fields(module_id="mod.gap2.legacy"),
        prompt_pack_hash="legacy-prompt-placeholder",
        tool_allowlist_hash="legacy-tool-placeholder",
        ui_panel_contract_hash=None,
        safety_policy_hash=None,
        budget_policy_hash=None,
    )

    entry = ModuleRegistry().register(legacy, ModuleLifecycle.SEED, "tenant")
    assert entry.module.prompt_pack_hash == "legacy-prompt-placeholder"
    assert entry.module.tool_allowlist_hash == "legacy-tool-placeholder"

    with pytest.raises(ValueError, match="artifact ref not blob-backed.*PROMPT_PACK"):
        ModuleRegistry(store).register(legacy, ModuleLifecycle.SEED, "tenant")

    sha_looking_legacy_forgery = HarnessModule.create(
        **_identity_fields(module_id="mod.gap2.legacy-forged-sha"),
        prompt_pack_hash=SHA_FORGED,
        tool_allowlist_hash="legacy-tool-placeholder",
        ui_panel_contract_hash=None,
        safety_policy_hash=None,
        budget_policy_hash=None,
    )

    with pytest.raises(ValueError, match="missing blob.*PROMPT_PACK"):
        ModuleRegistry(store).register(sha_looking_legacy_forgery, ModuleLifecycle.SEED, "tenant")
