import pytest

from ultron.evolution.variation import VariationEngine, VariationPrimitive
from ultron.hermes.capability import AdapterCapabilityContract, AttachSurface, CapabilitySpec, CapabilityStatus
from ultron.hermes.module_surface_contract import ModuleSurfaceContract
from ultron.module.model import HarnessModule, PersistencePolicy, PrivacyMetadata, PromotionState, TargetLens
from ultron.registry.store import ModuleLifecycle, ModuleRegistry


def _contract(topology_status=CapabilityStatus.DEFERRED):
    statuses = {surface: CapabilityStatus.SUPPORTED for surface in AttachSurface}
    statuses[AttachSurface.TOPOLOGY_SUBAGENT_CONTROL] = topology_status
    return AdapterCapabilityContract(
        hermes_commit="test",
        surfaces=[
            CapabilitySpec(surface=surface, status=status, rule="test")
            for surface, status in statuses.items()
        ],
    )


def _module(**overrides):
    data = {
        "module_id": "mod.evo",
        "name": "Evo",
        "version": 1,
        "workflow_tags": ["chat"],
        "target_lens": TargetLens.DEVELOPER,
        "owner_scope": "team-alpha",
        "surfaces": ModuleSurfaceContract(prompt_slots=["base"], tools=["read"]),
        "prompt_pack_hash": "sha256:prompt",
        "tool_allowlist_hash": "sha256:tools",
        "hermes_version_range": ">=test",
        "privacy": PrivacyMetadata(owner_scope="team-alpha"),
        "persistence_policy": PersistencePolicy.ISOLATED,
        "required_adapter_capabilities": [AttachSurface.PROMPT_SLOT_INJECTION],
    }
    data.update(overrides)
    return HarnessModule.create(**data)


def _engine(module=None, contract=None):
    registry = ModuleRegistry()
    parent = module or _module()
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    return VariationEngine(registry, contract or _contract()), registry, parent


def test_one_primitive_proposal_ok():
    engine, _, parent = _engine()

    proposal = engine.propose(parent.content_hash, VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "sha256:new"})

    assert proposal.parent_hash == parent.content_hash
    assert proposal.primitive == VariationPrimitive.PROMPT_SLOT_EDIT
    assert proposal.requires_human_approval is False


def test_compound_change_without_approval_raises():
    engine, _, parent = _engine()

    with pytest.raises(ValueError, match="compound"):
        engine.propose(
            parent.content_hash,
            VariationPrimitive.PROMPT_SLOT_EDIT,
            {"prompt_pack_hash": "sha256:new", "tool_allowlist_hash": "sha256:tools2"},
        )


def test_permission_expanding_change_requires_human_approval_and_apply_raises():
    engine, _, parent = _engine()

    proposal = engine.propose(
        parent.content_hash,
        VariationPrimitive.TOOLSET_TOGGLE,
        {"tools": ["read", "write"]},
    )

    assert proposal.requires_human_approval is True
    with pytest.raises(ValueError, match="requires human approval"):
        engine.apply(proposal)


def test_topology_change_deferred_is_rejected():
    engine, _, parent = _engine()

    with pytest.raises(ValueError, match="deferred"):
        engine.propose(
            parent.content_hash,
            VariationPrimitive.TOPOLOGY_CHANGE,
            {"topology_fragment_hash": "sha256:topology"},
            human_approved=True,
        )


def test_apply_yields_new_candidate_version_with_parent_and_recomputed_hash_registered_canary():
    engine, registry, parent = _engine()
    proposal = engine.propose(parent.content_hash, VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "sha256:new"})

    candidate = engine.apply(proposal)

    assert candidate.version == parent.version + 1
    assert candidate.parent_id == parent.content_hash
    assert candidate.content_hash != parent.content_hash
    assert candidate.content_hash == candidate.compute_content_hash()
    assert candidate.fitness.promotion_state == PromotionState.CANDIDATE
    entry = registry.get(candidate.content_hash)
    assert entry.lifecycle == ModuleLifecycle.CANDIDATE
    assert entry.layer == "canary"
