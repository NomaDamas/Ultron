import time

import pytest

from ultron.evolution.loop import EvolutionLoop, StabilityControls
from ultron.evolution.selection import SelectionOutcome, SelectionThresholds, Selector
from ultron.evolution.variation import VariationEngine, VariationPrimitive
from ultron.hermes.capability import AdapterCapabilityContract, AttachSurface, CapabilitySpec, CapabilityStatus
from ultron.hermes.module_surface_contract import ModuleSurfaceContract
from ultron.module.model import (
    EvidenceLabel,
    FitnessMetadata,
    HarnessModule,
    PersistencePolicy,
    PrivacyMetadata,
    PromotionState,
    TargetLens,
)
from ultron.registry.pointer import ActivePointerStore
from ultron.registry.store import ModuleLifecycle, ModuleRegistry


KEY = ("user", "workflow")


def _contract(topology_status=CapabilityStatus.SUPPORTED):
    statuses = {surface: CapabilityStatus.SUPPORTED for surface in AttachSurface}
    statuses[AttachSurface.TOPOLOGY_SUBAGENT_CONTROL] = topology_status
    return AdapterCapabilityContract(
        hermes_commit="test",
        surfaces=[CapabilitySpec(surface=surface, status=status, rule="test") for surface, status in statuses.items()],
    )


def _module(**overrides):
    data = {
        "module_id": "mod.g005.redteam",
        "name": "G005 Redteam",
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


def _candidate(parent, version, *, name=None, metric=None, decay=0.0, module_id=None):
    return _module(
        module_id=module_id or parent.module_id,
        version=version,
        parent_id=parent.content_hash,
        name=name or f"Candidate {version}",
        prompt_pack_hash=f"sha256:prompt-{version}",
        fitness=FitnessMetadata(primary_metric=metric, decay_score=decay, promotion_state=PromotionState.CANDIDATE),
    )


def _registered_loop(controls=None):
    registry = ModuleRegistry()
    pointer = ActivePointerStore()
    selector = Selector(SelectionThresholds())
    loop = EvolutionLoop(registry, pointer, selector, controls or StabilityControls())
    return registry, pointer, loop


def _engine(parent=None, contract=None):
    registry = ModuleRegistry()
    parent = parent or _module()
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    return VariationEngine(registry, contract or _contract()), registry, parent


def _outcome(hash_, *, label=EvidenceLabel.BENCHMARK, promotable=True, delta=0.2, paired=12, breaches=None):
    return SelectionOutcome(
        candidate_hash=hash_,
        evidence_label=label,
        primary_delta=delta,
        paired_tasks=paired,
        guardrail_breaches=list(breaches or []),
        promotable=promotable,
        rationale="redteam",
    )


def test_compound_change_requires_approval_but_approved_compound_is_allowed():
    engine, _, parent = _engine()
    compound = {"prompt_pack_hash": "sha256:new", "tool_allowlist_hash": "sha256:new-tools"}

    with pytest.raises(ValueError, match="compound"):
        engine.propose(parent.content_hash, VariationPrimitive.PROMPT_SLOT_EDIT, compound)

    proposal = engine.propose(parent.content_hash, VariationPrimitive.PROMPT_SLOT_EDIT, compound, human_approved=True)
    assert proposal.human_approved is True
    candidate = engine.apply(proposal)
    assert candidate.prompt_pack_hash == "sha256:new"
    assert candidate.tool_allowlist_hash == "sha256:new-tools"


def test_permission_expanding_change_requires_approval_and_only_applies_after_approval():
    engine, registry, parent = _engine()

    proposal = engine.propose(parent.content_hash, VariationPrimitive.TOOLSET_TOGGLE, {"tools": ["read", "write"]})

    assert proposal.requires_human_approval is True
    assert proposal.human_approved is False
    with pytest.raises(ValueError, match="requires human approval"):
        engine.apply(proposal)

    approved = proposal.model_copy(update={"human_approved": True})
    candidate = engine.apply(approved)
    assert candidate.surfaces.tools == ["read", "write"]
    entry = registry.get(candidate.content_hash)
    assert entry.lifecycle == ModuleLifecycle.CANDIDATE
    assert entry.layer == "canary"
    assert entry.human_approved_additive is True


def test_looser_persistence_policy_requires_approval_and_applies_only_after_approval():
    parent = _module(persistence_policy=PersistencePolicy.READ_ONLY)
    engine, _, _ = _engine(parent=parent)
    proposal = engine.propose(
        parent.content_hash,
        VariationPrimitive.SAFETY_TIGHTEN,
        {"persistence_policy": PersistencePolicy.NORMAL},
        human_approved=True,
    )

    assert proposal.requires_human_approval is True
    with pytest.raises(ValueError, match="requires human approval"):
        engine.apply(proposal.model_copy(update={"human_approved": False}))
    assert engine.apply(proposal).persistence_policy == PersistencePolicy.NORMAL


def test_topology_change_on_deferred_adapter_contract_is_rejected_even_when_approved():
    engine, _, parent = _engine(contract=_contract(CapabilityStatus.DEFERRED))

    with pytest.raises(ValueError, match="deferred"):
        engine.propose(
            parent.content_hash,
            VariationPrimitive.TOPOLOGY_CHANGE,
            {"topology_fragment_hash": "sha256:topology"},
            human_approved=True,
        )


def test_applied_candidate_has_lineage_recomputed_hash_and_canary_lifecycle():
    engine, registry, parent = _engine()
    proposal = engine.propose(parent.content_hash, VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "sha256:new"})

    candidate = engine.apply(proposal)

    assert candidate.parent_id == parent.content_hash
    assert candidate.version == parent.version + 1
    assert candidate.content_hash != parent.content_hash
    assert candidate.content_hash == candidate.compute_content_hash()
    assert candidate.fitness.promotion_state == PromotionState.CANDIDATE
    entry = registry.get(candidate.content_hash)
    assert entry.lifecycle == ModuleLifecycle.CANDIDATE
    assert entry.layer == "canary"


@pytest.mark.parametrize(
    ("paired", "candidate_metric", "guardrails_before", "guardrails_after", "expected_label", "expected_promotable"),
    [
        (10, 1.10, {"errors": 0.0}, {"errors": 0.0}, EvidenceLabel.BENCHMARK, True),
        (9, 1.50, {}, {}, EvidenceLabel.PREFERENCE, False),
        (10, 1.50, {"errors": 0.0}, {"errors": 0.1}, EvidenceLabel.INSUFFICIENT, False),
        (10, 1.09, {}, {}, EvidenceLabel.INSUFFICIENT, False),
    ],
)
def test_selection_truth_table_and_never_promotes_preference_or_insufficient(
    paired, candidate_metric, guardrails_before, guardrails_after, expected_label, expected_promotable
):
    outcome = Selector(SelectionThresholds()).evaluate(
        "candidate", 1.0, candidate_metric, paired, guardrails_before, guardrails_after
    )

    assert outcome.evidence_label == expected_label
    assert outcome.promotable is expected_promotable
    if outcome.evidence_label in {EvidenceLabel.PREFERENCE, EvidenceLabel.INSUFFICIENT}:
        assert outcome.promotable is False


def test_retain_does_not_advance_pointer_for_non_promotable_and_advances_promotable_via_cas():
    registry, pointer, loop = _registered_loop()
    parent = _module()
    loser = _candidate(parent, 2)
    winner = _candidate(parent, 3)
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    registry.register(loser, ModuleLifecycle.CANDIDATE, "canary")
    registry.register(winner, ModuleLifecycle.CANDIDATE, "canary")
    assert pointer.swap(KEY, 0, [parent.content_hash]) == 1

    assert loop.retain(
        loser.content_hash,
        _outcome(loser.content_hash, label=EvidenceLabel.INSUFFICIENT, promotable=False, delta=0.05),
        *KEY,
        1,
    ) is False
    assert pointer.get(KEY) == (1, [parent.content_hash])
    assert registry.get(loser.content_hash).lifecycle == ModuleLifecycle.DECAYING

    assert loop.retain(winner.content_hash, _outcome(winner.content_hash), *KEY, 1) is True
    version, active = pointer.get(KEY)
    assert version == 2
    assert active == [parent.content_hash, winner.content_hash]
    assert registry.get(winner.content_hash).lifecycle == ModuleLifecycle.SURVIVOR

    stale = _candidate(parent, 4)
    registry.register(stale, ModuleLifecycle.CANDIDATE, "canary")
    with pytest.raises(ValueError, match="stale active pointer version"):
        loop.retain(stale.content_hash, _outcome(stale.content_hash), *KEY, 1)


def test_active_module_cap_eviction_is_reversible_and_count_never_exceeds_cap():
    registry, pointer, loop = _registered_loop(StabilityControls(active_module_cap=2, diversity_floor=1))
    parent = _module()
    weak = _candidate(parent, 2, name="Weak", metric=0.1)
    strong = _candidate(parent, 3, name="Strong", metric=0.9)
    promoted = _candidate(parent, 4, name="Promoted", metric=1.0)
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    for module in [weak, strong, promoted]:
        registry.register(module, ModuleLifecycle.CANDIDATE, "canary")
    registry.set_lifecycle(weak.content_hash, ModuleLifecycle.SURVIVOR)
    registry.set_lifecycle(strong.content_hash, ModuleLifecycle.SURVIVOR)
    assert pointer.swap(KEY, 0, [weak.content_hash, strong.content_hash]) == 1

    assert loop.retain(promoted.content_hash, _outcome(promoted.content_hash), *KEY, 1) is True

    version, active = pointer.get(KEY)
    assert version == 2
    assert len(active) <= 2
    assert promoted.content_hash in active
    assert strong.content_hash in active
    assert weak.content_hash not in active
    assert registry.get(weak.content_hash).lifecycle == ModuleLifecycle.PRUNED
    assert registry.lineage(weak.content_hash)[0].module.content_hash == weak.content_hash

    assert loop.restore(weak.content_hash, *KEY, 2) is True
    version, restored_active = pointer.get(KEY)
    assert version == 3
    assert len(restored_active) <= 2
    assert weak.content_hash in restored_active
    assert registry.get(weak.content_hash).lifecycle == ModuleLifecycle.SURVIVOR


def test_atrophy_scan_and_prune_refuse_to_breach_diversity_floor():
    registry, pointer, loop = _registered_loop(StabilityControls(diversity_floor=2))
    modules = [
        _module(
            version=i,
            name=f"Floor {i}",
            prompt_pack_hash=f"sha256:floor-{i}",
            fitness=FitnessMetadata(primary_metric=-1.0, usage_count=0, last_used_at=1.0),
        )
        for i in range(1, 4)
    ]
    for module in modules:
        registry.register(module, ModuleLifecycle.SURVIVOR, "tenant")
    assert pointer.swap(KEY, 0, [m.content_hash for m in modules]) == 1

    eligible = loop.atrophy_scan([m.content_hash for m in modules], time.time())
    assert len(eligible) == 1
    assert len(modules) - len(eligible) >= 2

    assert loop.prune(modules[0].content_hash) is True
    assert pointer.get(KEY)[1] == [modules[1].content_hash, modules[2].content_hash]
    with pytest.raises(ValueError, match="diversity floor"):
        loop.prune(modules[1].content_hash)
    assert pointer.get(KEY)[1] == [modules[1].content_hash, modules[2].content_hash]


def test_critical_seed_prune_requires_approval_preserves_registry_history_and_restores_survivor():
    registry, pointer, loop = _registered_loop(StabilityControls(diversity_floor=1))
    modules = [_module(version=i, name=f"Critical {i}", prompt_pack_hash=f"sha256:critical-{i}") for i in range(1, 4)]
    for module in modules:
        registry.register(module, ModuleLifecycle.SURVIVOR, "tenant")
    assert pointer.swap(KEY, 0, [m.content_hash for m in modules]) == 1

    with pytest.raises(ValueError, match="critical seed"):
        loop.prune(modules[0].content_hash, is_critical_seed=True)

    assert loop.prune(modules[0].content_hash, is_critical_seed=True, approved=True) is True
    assert registry.get(modules[0].content_hash).lifecycle == ModuleLifecycle.PRUNED
    assert modules[0].content_hash not in pointer.get(KEY)[1]
    assert registry.lineage(modules[0].content_hash)[0].module.content_hash == modules[0].content_hash

    assert loop.restore(modules[0].content_hash, *KEY, 2) is True
    assert modules[0].content_hash in pointer.get(KEY)[1]
    assert registry.get(modules[0].content_hash).lifecycle == ModuleLifecycle.SURVIVOR


def test_variant_budget_excess_for_same_parent_is_rejected_and_candidate_decays():
    registry, _, loop = _registered_loop(StabilityControls(variant_budget=1))
    parent = _module()
    first = _candidate(parent, 2)
    second = _candidate(parent, 3)
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    registry.register(first, ModuleLifecycle.CANDIDATE, "canary")
    registry.register(second, ModuleLifecycle.CANDIDATE, "canary")

    with pytest.raises(ValueError, match="variant budget"):
        loop.register_candidate(second.content_hash)
    assert registry.get(second.content_hash).lifecycle == ModuleLifecycle.DECAYING
