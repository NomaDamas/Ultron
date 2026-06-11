import time

import pytest

from ultron.evolution.loop import EvolutionLoop, StabilityControls
from ultron.evolution.selection import SelectionOutcome, SelectionThresholds, Selector
from ultron.hermes.capability import AttachSurface
from ultron.hermes.module_surface_contract import ModuleSurfaceContract
from ultron.module.model import EvidenceLabel, FitnessMetadata, HarnessModule, PrivacyMetadata, PromotionState, TargetLens
from ultron.registry.pointer import ActivePointerStore
from ultron.registry.store import ModuleLifecycle, ModuleRegistry


def _module(**overrides):
    data = {
        "module_id": "mod.loop",
        "name": "Loop",
        "version": 1,
        "workflow_tags": ["chat"],
        "target_lens": TargetLens.DEVELOPER,
        "owner_scope": "team-alpha",
        "surfaces": ModuleSurfaceContract(prompt_slots=["base"], tools=["read"]),
        "prompt_pack_hash": "sha256:prompt",
        "tool_allowlist_hash": "sha256:tools",
        "hermes_version_range": ">=test",
        "privacy": PrivacyMetadata(owner_scope="team-alpha"),
        "required_adapter_capabilities": [AttachSurface.PROMPT_SLOT_INJECTION],
    }
    data.update(overrides)
    return HarnessModule.create(**data)


def _candidate(parent, version, name=None, metric=None, decay=0.0):
    return _module(
        version=version,
        parent_id=parent.content_hash,
        name=name or f"Candidate {version}",
        prompt_pack_hash=f"sha256:prompt-{version}",
        fitness=FitnessMetadata(primary_metric=metric, decay_score=decay, promotion_state=PromotionState.CANDIDATE),
    )


def _loop(controls=None):
    registry = ModuleRegistry()
    pointer = ActivePointerStore()
    selector = Selector(SelectionThresholds())
    return registry, pointer, EvolutionLoop(registry, pointer, selector, controls or StabilityControls())


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


def _not_promotable(hash_):
    return _promotable(hash_).model_copy(update={"evidence_label": EvidenceLabel.INSUFFICIENT, "promotable": False})


def test_variant_budget_enforced():
    registry, _, loop = _loop(StabilityControls(variant_budget=1))
    parent = _module()
    c1 = _candidate(parent, 2)
    c2 = _candidate(parent, 3)
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    registry.register(c1, ModuleLifecycle.CANDIDATE, "canary")
    assert loop.register_candidate(c1.content_hash) is True
    registry.register(c2, ModuleLifecycle.CANDIDATE, "canary")

    with pytest.raises(ValueError, match="variant budget"):
        loop.register_candidate(c2.content_hash)


def test_retain_promotes_via_cas_and_eviction_is_reversible_under_cap():
    registry, pointer, loop = _loop(StabilityControls(active_module_cap=2, diversity_floor=1))
    parent = _module()
    old = _candidate(parent, 2, metric=0.1)
    keep = _candidate(parent, 3, metric=0.9)
    cand = _candidate(parent, 4, metric=1.0)
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    for module in [old, keep, cand]:
        registry.register(module, ModuleLifecycle.SURVIVOR if module is not cand else ModuleLifecycle.CANDIDATE, "canary")
    assert pointer.swap(("u", "wf"), 0, [old.content_hash, keep.content_hash]) == 1

    assert loop.retain(cand.content_hash, _promotable(cand.content_hash), "u", "wf", 1) is True

    version, active = pointer.get(("u", "wf"))
    assert version == 2
    assert cand.content_hash in active
    assert keep.content_hash in active
    assert old.content_hash not in active
    assert registry.get(old.content_hash).lifecycle == ModuleLifecycle.PRUNED
    assert registry.get(cand.content_hash).lifecycle == ModuleLifecycle.SURVIVOR
    assert loop.restore(old.content_hash, "u", "wf", 2) is True
    assert registry.get(old.content_hash).lifecycle == ModuleLifecycle.SURVIVOR


def test_non_promotable_not_retained():
    registry, pointer, loop = _loop()
    parent = _module()
    cand = _candidate(parent, 2)
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    registry.register(cand, ModuleLifecycle.CANDIDATE, "canary")

    assert loop.retain(cand.content_hash, _not_promotable(cand.content_hash), "u", "wf", 0) is False
    assert pointer.get(("u", "wf")) == (0, [])
    assert registry.get(cand.content_hash).lifecycle == ModuleLifecycle.DECAYING


def test_atrophy_scan_respects_diversity_floor_and_critical_seed():
    registry, _, loop = _loop(StabilityControls(diversity_floor=2))
    parent = _module()
    modules = [
        _module(module_id="mod.loop", version=i, name=f"M{i}", prompt_pack_hash=f"sha256:{i}", fitness=FitnessMetadata(primary_metric=-1.0))
        for i in range(1, 5)
    ]
    for module in modules:
        registry.register(module, ModuleLifecycle.SURVIVOR, "tenant")
    loop.mark_critical_seed(modules[0].content_hash)

    eligible = loop.atrophy_scan([m.content_hash for m in modules], time.time())

    assert modules[0].content_hash not in eligible
    assert len(eligible) == 2


def test_prune_reversible_restore_and_critical_seed_requires_approval():
    registry, pointer, loop = _loop(StabilityControls(diversity_floor=1))
    modules = [_module(version=i, name=f"M{i}", prompt_pack_hash=f"sha256:{i}") for i in range(1, 4)]
    for module in modules:
        registry.register(module, ModuleLifecycle.SURVIVOR, "tenant")
    assert pointer.swap(("u", "wf"), 0, [m.content_hash for m in modules]) == 1

    with pytest.raises(ValueError, match="critical seed"):
        loop.prune(modules[0].content_hash, is_critical_seed=True)

    assert loop.prune(modules[0].content_hash, is_critical_seed=True, approved=True) is True
    assert modules[0].content_hash not in pointer.get(("u", "wf"))[1]
    assert registry.get(modules[0].content_hash).lifecycle == ModuleLifecycle.PRUNED
    assert loop.restore(modules[0].content_hash, "u", "wf", 2) is True
    assert modules[0].content_hash in pointer.get(("u", "wf"))[1]
    assert registry.get(modules[0].content_hash).lifecycle == ModuleLifecycle.SURVIVOR


def test_cooldown_respected():
    registry, pointer, loop = _loop(StabilityControls(promotion_cooldown_s=60, prune_cooldown_s=60, diversity_floor=1))
    parent = _module()
    c1 = _candidate(parent, 2)
    c2 = _candidate(parent, 3)
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    registry.register(c1, ModuleLifecycle.CANDIDATE, "canary")
    registry.register(c2, ModuleLifecycle.CANDIDATE, "canary")

    assert loop.retain(c1.content_hash, _promotable(c1.content_hash), "u", "wf", 0) is True
    with pytest.raises(ValueError, match="promotion cooldown"):
        loop.retain(c2.content_hash, _promotable(c2.content_hash), "u", "wf", 1)

    pointer.swap(("u2", "wf"), 0, [parent.content_hash, c1.content_hash])
    loop.prune(parent.content_hash)
    pointer.swap(("u2", "wf"), 2, [parent.content_hash, c1.content_hash])
    with pytest.raises(ValueError, match="prune cooldown"):
        loop.prune(parent.content_hash)
