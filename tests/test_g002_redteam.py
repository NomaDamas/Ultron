from copy import deepcopy

import pytest
from pydantic import ValidationError

from ultron.hermes.capability import AdapterCapabilityContract, AttachSurface
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


CONTRACT_PATH = __import__("pathlib").Path(__file__).resolve().parents[1] / "adapter_capability_contract.yaml"


def _contract() -> AdapterCapabilityContract:
    return AdapterCapabilityContract.from_yaml(CONTRACT_PATH)


def _module_data(**overrides):
    data = {
        "module_id": "mod.prompt-tools",
        "name": "Prompt Tools",
        "version": 1,
        "parent_id": "mod.parent",
        "workflow_tags": ["chat", "tools"],
        "target_lens": TargetLens.DEVELOPER,
        "owner_scope": "team-alpha",
        "surfaces": ModuleSurfaceContract(
            prompt_slots=["HERMES.md", "SOUL.md"],
            tools=["read", "write"],
            skill_refs=["skill:debug"],
            safety={"guardrails": {"pii": "redact", "toxicity": "block"}},
            budget={"limits": {"tokens": 1000, "wall_seconds": 30}},
            persistence={"mode": "isolated", "ttl_days": 7},
        ),
        "prompt_pack_hash": "sha256:prompt",
        "tool_allowlist_hash": "sha256:tools",
        "skill_refs": ["skill:debug", "skill:review"],
        "topology_fragment_hash": "sha256:topology",
        "ui_panel_contract_hash": "sha256:ui",
        "safety_policy_hash": "sha256:safety",
        "budget_policy_hash": "sha256:budget",
        "persistence_policy": PersistencePolicy.ISOLATED,
        "required_adapter_capabilities": [
            AttachSurface.PROMPT_SLOT_INJECTION,
            AttachSurface.TOOL_TOOLSET_ALLOWLIST,
            AttachSurface.SKILL_REFERENCE,
            AttachSurface.BUDGET_ENFORCEMENT,
            AttachSurface.MEMORY_SKILL_ISOLATION,
        ],
        "hermes_version_range": ">=ee1a744,<ff000000",
        "privacy": PrivacyMetadata(
            owner_scope="team-alpha",
            consent_class="operational",
            global_template_eligible=False,
            redaction_status="none",
            retention_rule="default",
        ),
    }
    data.update(overrides)
    return data


def _module(**overrides) -> HarnessModule:
    return HarnessModule.create(**_module_data(**overrides))


def test_g002_redteam_content_hash_canonicalizes_field_and_dict_key_order():
    first = _module()
    reordered_data = {}
    for key in reversed(list(_module_data())):
        reordered_data[key] = _module_data()[key]
    reordered_data["surfaces"] = ModuleSurfaceContract(
        persistence={"ttl_days": 7, "mode": "isolated"},
        budget={"limits": {"wall_seconds": 30, "tokens": 1000}},
        safety={"guardrails": {"toxicity": "block", "pii": "redact"}},
        skill_refs=["skill:debug"],
        tools=["read", "write"],
        prompt_slots=["HERMES.md", "SOUL.md"],
    )

    second = HarnessModule.create(**reordered_data)

    assert second.model_dump(mode="json", exclude={"content_hash"}) == first.model_dump(
        mode="json", exclude={"content_hash"}
    )
    assert second.content_hash == first.content_hash
    assert second.compute_content_hash() == first.content_hash


def test_g002_redteam_content_hash_ignores_runtime_fitness_metadata():
    base = _module()
    for fitness in [
        FitnessMetadata(usage_count=99),
        FitnessMetadata(last_used_at=9876543210.0),
        FitnessMetadata(decay_score=0.875),
        FitnessMetadata(evidence_labels=[EvidenceLabel.PREFERENCE, EvidenceLabel.BENCHMARK]),
        FitnessMetadata(promotion_state=PromotionState.SURVIVOR),
        FitnessMetadata(
            primary_metric=0.91,
            guardrails={"latency": 1.2},
            usage_count=12,
            last_used_at=123.0,
            decay_score=0.25,
            evidence_labels=[EvidenceLabel.CAUSAL_SUFFICIENT, EvidenceLabel.INSUFFICIENT],
            promotion_state=PromotionState.QUARANTINED,
        ),
    ]:
        mutated = base.model_copy(update={"fitness": fitness})
        assert mutated.compute_content_hash() == base.content_hash
        assert mutated.finalized().content_hash == base.content_hash


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Prompt Tools renamed"),
        ("version", 2),
        ("owner_scope", "team-beta"),
        ("prompt_pack_hash", "sha256:prompt-v2"),
        ("tool_allowlist_hash", "sha256:tools-v2"),
        ("topology_fragment_hash", "sha256:topology-v2"),
        ("ui_panel_contract_hash", "sha256:ui-v2"),
        ("safety_policy_hash", "sha256:safety-v2"),
        ("budget_policy_hash", "sha256:budget-v2"),
        ("persistence_policy", PersistencePolicy.CHECKPOINTED),
        ("surfaces", ModuleSurfaceContract(prompt_slots=["HERMES.md"], tools=["read", "write", "bash"])),
        ("workflow_tags", ["chat", "tools", "review"]),
        ("target_lens", TargetLens.OPS),
        ("required_adapter_capabilities", [AttachSurface.PROMPT_SLOT_INJECTION]),
        ("hermes_version_range", ">=ff000000"),
        ("privacy", PrivacyMetadata(owner_scope="team-alpha", consent_class="research")),
        ("privacy", PrivacyMetadata(owner_scope="team-alpha", global_template_eligible=True)),
        ("privacy", PrivacyMetadata(owner_scope="team-alpha", redaction_status="redacted")),
        ("privacy", PrivacyMetadata(owner_scope="team-alpha", retention_rule="ephemeral")),
    ],
)
def test_g002_redteam_identity_field_mutations_change_content_hash(field, value):
    base = _module()
    mutated = _module(**{field: value})

    assert mutated.content_hash != base.content_hash


def test_g002_redteam_distinct_design_collision_probe_has_unique_hashes():
    modules = [
        _module(
            module_id=f"mod.design-{index}",
            name=f"Design {index}",
            version=1 + (index % 5),
            workflow_tags=["chat", f"variant-{index % 7}"],
            owner_scope=f"team-{index % 11}",
            prompt_pack_hash=f"sha256:prompt-{index}",
            tool_allowlist_hash=f"sha256:tools-{index % 13}",
            safety_policy_hash=f"sha256:safety-{index % 17}",
            budget_policy_hash=f"sha256:budget-{index % 19}",
            hermes_version_range=f">=ee1a{index:04x}",
            privacy=PrivacyMetadata(
                owner_scope=f"team-{index % 11}",
                consent_class="operational" if index % 2 else "research",
                global_template_eligible=index % 3 == 0,
                redaction_status="none" if index % 4 else "redacted",
                retention_rule=f"retention-{index % 5}",
            ),
        )
        for index in range(100)
    ]
    hashes = [module.content_hash for module in modules]

    assert len(hashes) == len(set(hashes))


def test_g002_redteam_json_round_trip_preserves_content_hash_and_finalized_is_idempotent():
    module = _module()
    decoded = HarnessModule.model_validate_json(module.model_dump_json())
    finalized_once = module.finalized()
    finalized_twice = finalized_once.finalized()

    assert decoded == module
    assert decoded.content_hash == module.content_hash
    assert decoded.compute_content_hash() == module.content_hash
    assert finalized_once == module
    assert finalized_twice == finalized_once


@pytest.mark.parametrize("version", [0, -1])
def test_g002_redteam_version_must_be_positive(version):
    with pytest.raises(ValidationError):
        HarnessModule.model_validate(_module_data(version=version))


def test_g002_redteam_validate_surfaces_rejects_deferred_topology_fragment():
    module = _module(
        surfaces=ModuleSurfaceContract(topology_fragment={"workers": 2}),
        required_adapter_capabilities=[],
    )

    with pytest.raises(ValueError, match="topology_fragment: attach surface is deferred: topology-subagent-control"):
        module.validate_surfaces(_contract())


def test_g002_redteam_validate_surfaces_rejects_deferred_required_adapter_capability():
    module = _module(required_adapter_capabilities=[AttachSurface.TOPOLOGY_SUBAGENT_CONTROL])

    with pytest.raises(ValueError, match="required adapter capability is deferred: topology-subagent-control"):
        module.validate_surfaces(_contract())


def test_g002_redteam_validate_surfaces_accepts_clean_prompt_slots_and_tools():
    module = _module(
        surfaces=ModuleSurfaceContract(prompt_slots=["HERMES.md"], tools=["read"]),
        required_adapter_capabilities=[
            AttachSurface.PROMPT_SLOT_INJECTION,
            AttachSurface.TOOL_TOOLSET_ALLOWLIST,
        ],
    )

    assert module.validate_surfaces(_contract()) is None


@pytest.mark.parametrize(
    "prohibited_surface",
    ["global_memory_write", "hermes_source_mutation", "credential_mutation"],
)
def test_g002_redteam_preserved_core_prohibited_surface_rejected_at_construction(prohibited_surface):
    with pytest.raises(ValidationError) as excinfo:
        ModuleSurfaceContract.model_validate({prohibited_surface: True})

    assert prohibited_surface in str(excinfo.value)


def test_g002_redteam_enum_string_values_are_exact():
    assert [label.value for label in EvidenceLabel] == [
        "preference_evidence",
        "benchmark_evidence",
        "causal_sufficient_for_mvp",
        "insufficient_evidence",
    ]
