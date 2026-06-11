import pytest

from ultron.evolution.loop import EvolutionLoop, StabilityControls
from ultron.evolution.selection import SelectionOutcome, SelectionThresholds, Selector
from ultron.evolution.variation import MutationProposal, VariationEngine, VariationPrimitive
from ultron.hermes.capability import AdapterCapabilityContract, AttachSurface, CapabilitySpec, CapabilityStatus
from ultron.hermes.module_surface_contract import ModuleSurfaceContract
from ultron.module.model import EvidenceLabel, FitnessMetadata, HarnessModule, PersistencePolicy, PrivacyMetadata, PromotionState, TargetLens
from ultron.registry.pointer import ActivePointerStore
from ultron.registry.store import ModuleLifecycle, ModuleRegistry


KEY = ("user", "workflow")


class FailingSwapPointerStore(ActivePointerStore):
    def swap(self, key, expected_version, new_hashes):
        raise ValueError("forced cas failure")


def _contract(topology_status=CapabilityStatus.DEFERRED):
    statuses = {surface: CapabilityStatus.SUPPORTED for surface in AttachSurface}
    statuses[AttachSurface.TOPOLOGY_SUBAGENT_CONTROL] = topology_status
    return AdapterCapabilityContract(
        hermes_commit="test",
        surfaces=[CapabilitySpec(surface=surface, status=status, rule="test") for surface, status in statuses.items()],
    )


def _module(**overrides):
    data = {
        "module_id": "mod.g005.hardening",
        "name": "G005 Hardening",
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


def _candidate(parent, version, *, metric=None, prompt=None):
    return _module(
        version=version,
        parent_id=parent.content_hash,
        name=f"Candidate {version}",
        prompt_pack_hash=prompt or f"sha256:prompt-{version}",
        fitness=FitnessMetadata(primary_metric=metric, promotion_state=PromotionState.CANDIDATE),
    )


def _loop(controls=None, pointer=None):
    registry = ModuleRegistry()
    pointer = pointer or ActivePointerStore()
    selector = Selector(SelectionThresholds())
    return registry, pointer, EvolutionLoop(registry, pointer, selector, controls or StabilityControls())


def _engine(parent=None, contract=None):
    registry = ModuleRegistry()
    parent = parent or _module()
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    return VariationEngine(registry, contract or _contract()), registry, parent


def _promotable(hash_):
    return SelectionOutcome(
        candidate_hash=hash_,
        evidence_label=EvidenceLabel.BENCHMARK,
        primary_delta=0.2,
        paired_tasks=12,
        guardrail_breaches=[],
        promotable=True,
        rationale="ok",
    )


def test_forged_preference_promotable_outcome_cannot_advance_pointer():
    registry, pointer, loop = _loop()
    parent = _module()
    cand = _candidate(parent, 2)
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    registry.register(cand, ModuleLifecycle.CANDIDATE, "canary")
    pointer.swap(KEY, 0, [parent.content_hash])

    with pytest.raises(ValueError, match="promotable must be derived"):
        SelectionOutcome(
            candidate_hash=cand.content_hash,
            evidence_label=EvidenceLabel.PREFERENCE,
            primary_delta=0.5,
            paired_tasks=1,
            guardrail_breaches=[],
            promotable=True,
            rationale="forged",
        )

    stale_forged = _promotable(cand.content_hash).model_construct(
        candidate_hash=cand.content_hash,
        evidence_label=EvidenceLabel.PREFERENCE,
        primary_delta=0.5,
        paired_tasks=1,
        guardrail_breaches=[],
        promotable=True,
        rationale="forged",
    )
    assert loop.retain(cand.content_hash, stale_forged, *KEY, 1) is False
    assert pointer.get(KEY) == (1, [parent.content_hash])


def test_forged_mutation_proposal_flags_are_revalidated_by_apply():
    engine, _, parent = _engine()
    forged_compound = MutationProposal(
        parent_hash=parent.content_hash,
        primitive=VariationPrimitive.PROMPT_SLOT_EDIT,
        change={"prompt_pack_hash": "sha256:new", "tool_allowlist_hash": "sha256:new-tools"},
        rationale="forged",
        requires_human_approval=False,
        human_approved=False,
    )
    with pytest.raises(ValueError, match="requires human approval"):
        engine.apply(forged_compound)

    forged_permission = MutationProposal(
        parent_hash=parent.content_hash,
        primitive=VariationPrimitive.TOOLSET_TOGGLE,
        change={"tools": ["read", "write"]},
        rationale="forged",
        requires_human_approval=False,
        human_approved=False,
    )
    with pytest.raises(ValueError, match="requires human approval"):
        engine.apply(forged_permission)


def test_topology_fragment_under_prompt_slot_edit_on_deferred_contract_rejected():
    engine, _, parent = _engine(contract=_contract(CapabilityStatus.DEFERRED))
    proposal = MutationProposal(
        parent_hash=parent.content_hash,
        primitive=VariationPrimitive.PROMPT_SLOT_EDIT,
        change={"topology_fragment_hash": "sha256:topology"},
        rationale="forged",
        requires_human_approval=False,
        human_approved=False,
    )
    with pytest.raises(ValueError, match="topology/deferred"):
        engine.apply(proposal)


def test_cap_eviction_refuses_to_prune_critical_seed():
    registry, pointer, loop = _loop(StabilityControls(active_module_cap=2, diversity_floor=1))
    parent = _module()
    critical = _candidate(parent, 2, metric=0.1)
    other = _candidate(parent, 3, metric=0.9)
    promoted = _candidate(parent, 4, metric=1.0)
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    for module in [critical, other, promoted]:
        registry.register(module, ModuleLifecycle.SURVIVOR if module is not promoted else ModuleLifecycle.CANDIDATE, "canary")
    pointer.swap(KEY, 0, [critical.content_hash, other.content_hash])
    loop.mark_critical_seed(critical.content_hash)

    with pytest.raises(ValueError, match="critical seed"):
        loop.retain(promoted.content_hash, _promotable(promoted.content_hash), *KEY, 1)
    assert registry.get(critical.content_hash).lifecycle == ModuleLifecycle.SURVIVOR
    assert pointer.get(KEY) == (1, [critical.content_hash, other.content_hash])


def test_restore_cas_failure_leaves_lifecycle_unchanged():
    pointer = FailingSwapPointerStore()
    registry, _, loop = _loop(StabilityControls(active_module_cap=2, diversity_floor=1), pointer=pointer)
    parent = _module()
    evicted = _candidate(parent, 2, metric=0.1)
    restored = _candidate(parent, 3, metric=0.9)
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    registry.register(evicted, ModuleLifecycle.SURVIVOR, "canary")
    registry.register(restored, ModuleLifecycle.PRUNED, "canary")
    pointer._pointers[KEY] = (1, (evicted.content_hash,))

    with pytest.raises(ValueError, match="forced cas failure"):
        loop.restore(restored.content_hash, *KEY, 1)
    assert registry.get(restored.content_hash).lifecycle == ModuleLifecycle.PRUNED
    assert registry.get(evicted.content_hash).lifecycle == ModuleLifecycle.SURVIVOR


def test_prune_cooldown_recorded_on_cap_eviction():
    registry, pointer, loop = _loop(StabilityControls(active_module_cap=2, diversity_floor=1, prune_cooldown_s=60))
    parent = _module()
    weak = _candidate(parent, 2, metric=0.1)
    strong = _candidate(parent, 3, metric=0.9)
    promoted = _candidate(parent, 4, metric=1.0)
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    for module in [weak, strong, promoted]:
        registry.register(module, ModuleLifecycle.SURVIVOR if module is not promoted else ModuleLifecycle.CANDIDATE, "canary")
    pointer.swap(KEY, 0, [weak.content_hash, strong.content_hash])

    assert loop.retain(promoted.content_hash, _promotable(promoted.content_hash), *KEY, 1) is True
    assert weak.content_hash in loop._last_prune_at
    with pytest.raises(ValueError, match="prune cooldown"):
        loop.prune(weak.content_hash)
