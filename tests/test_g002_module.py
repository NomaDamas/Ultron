from pathlib import Path

import pytest

from ultron.hermes.capability import AdapterCapabilityContract, AttachSurface
from ultron.hermes.module_surface_contract import ModuleSurfaceContract
from ultron.module.contract import load_default_contract, validate_declared_surfaces
from ultron.module.model import (
    EvidenceLabel,
    FitnessMetadata,
    HarnessModule,
    PersistencePolicy,
    PrivacyMetadata,
    PromotionState,
    TargetLens,
)


CONTRACT_PATH = Path(__file__).resolve().parents[1] / "adapter_capability_contract.yaml"


def _contract():
    return AdapterCapabilityContract.from_yaml(CONTRACT_PATH)


def _module(**overrides):
    data = {
        "module_id": "mod.prompt-tools",
        "name": "Prompt Tools",
        "version": 1,
        "parent_id": None,
        "workflow_tags": ["chat", "tools"],
        "target_lens": TargetLens.DEVELOPER,
        "owner_scope": "team-alpha",
        "surfaces": ModuleSurfaceContract(prompt_slots=["HERMES.md"], tools=["read"]),
        "prompt_pack_hash": "sha256:prompt",
        "tool_allowlist_hash": "sha256:tools",
        "skill_refs": ["skill:debug"],
        "topology_fragment_hash": None,
        "ui_panel_contract_hash": None,
        "safety_policy_hash": "sha256:safety",
        "budget_policy_hash": "sha256:budget",
        "persistence_policy": PersistencePolicy.ISOLATED,
        "required_adapter_capabilities": [
            AttachSurface.PROMPT_SLOT_INJECTION,
            AttachSurface.TOOL_TOOLSET_ALLOWLIST,
        ],
        "hermes_version_range": ">=ee1a744",
        "privacy": PrivacyMetadata(owner_scope="team-alpha"),
    }
    data.update(overrides)
    return HarnessModule.create(**data)


def test_content_hash_determinism_across_constructions_and_canonical_dump():
    first = _module()
    second = _module()

    assert first.content_hash == second.content_hash
    assert first.compute_content_hash() == first.content_hash

    round_tripped = HarnessModule.model_validate_json(first.model_dump_json())
    assert round_tripped.compute_content_hash() == first.content_hash


def test_content_hash_excludes_fitness_usage_and_promotion_state():
    original = _module()
    mutated = original.model_copy(
        update={
            "fitness": FitnessMetadata(
                primary_metric=0.95,
                guardrails={"latency": 1.5},
                usage_count=42,
                last_used_at=1234567890.0,
                decay_score=0.7,
                evidence_labels=[EvidenceLabel.BENCHMARK],
                promotion_state=PromotionState.SURVIVOR,
            )
        }
    )

    assert mutated.compute_content_hash() == original.content_hash
    assert mutated.finalized().content_hash == original.content_hash


def test_identity_field_changes_change_content_hash():
    base = _module()

    assert _module(name="Prompt Tools v2").content_hash != base.content_hash
    assert _module(version=2).content_hash != base.content_hash
    assert _module(
        surfaces=ModuleSurfaceContract(prompt_slots=["HERMES.md"], tools=["read", "write"])
    ).content_hash != base.content_hash
    assert _module(prompt_pack_hash="sha256:new-prompt").content_hash != base.content_hash


def test_json_round_trip_equivalence_and_hash_stability():
    module = _module()
    encoded = module.model_dump_json()
    decoded = HarnessModule.model_validate_json(encoded)

    assert decoded == module
    assert decoded.content_hash == module.content_hash
    assert decoded.compute_content_hash() == module.content_hash


def test_validate_surfaces_rejects_deferred_declared_surface():
    module = _module(
        surfaces=ModuleSurfaceContract(topology_fragment={"workers": 2}),
        required_adapter_capabilities=[],
    )

    with pytest.raises(ValueError, match="deferred"):
        module.validate_surfaces(_contract())


def test_validate_surfaces_rejects_deferred_required_capability():
    module = _module(
        required_adapter_capabilities=[AttachSurface.TOPOLOGY_SUBAGENT_CONTROL],
    )

    with pytest.raises(ValueError, match="deferred"):
        module.validate_surfaces(_contract())


def test_validate_surfaces_accepts_clean_prompt_slots_and_tools_module():
    module = _module()

    module.validate_surfaces(_contract())
    assert validate_declared_surfaces(
        {"prompt_slots": ["HERMES.md"], "tools": ["read"]}, load_default_contract()
    ) == module.surfaces


def test_enums_serialize_to_specified_values():
    module = _module(
        target_lens=TargetLens.COMMUNITY,
        persistence_policy=PersistencePolicy.CHECKPOINTED,
        fitness=FitnessMetadata(
            evidence_labels=[EvidenceLabel.PREFERENCE, EvidenceLabel.CAUSAL_SUFFICIENT],
            promotion_state=PromotionState.CANDIDATE,
        ),
    )
    dumped = module.model_dump(mode="json")

    assert dumped["target_lens"] == "COMMUNITY"
    assert dumped["persistence_policy"] == "CHECKPOINTED"
    assert dumped["fitness"]["evidence_labels"] == [
        "preference_evidence",
        "causal_sufficient_for_mvp",
    ]
    assert dumped["fitness"]["promotion_state"] == "CANDIDATE"
