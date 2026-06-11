from pathlib import Path

from ultron.composition.resolver import CompositionResolver
from ultron.hermes.capability import AdapterCapabilityContract, AttachSurface
from ultron.hermes.module_surface_contract import ModuleSurfaceContract
from ultron.module.model import HarnessModule, PrivacyMetadata, TargetLens
from ultron.registry.store import ModuleLifecycle, ModuleRegistry


CONTRACT_PATH = Path(__file__).resolve().parents[1] / "adapter_capability_contract.yaml"


def _contract():
    return AdapterCapabilityContract.from_yaml(CONTRACT_PATH)


def _module(module_id, version=1, **overrides):
    data = {
        "module_id": module_id,
        "name": module_id,
        "version": version,
        "parent_id": None,
        "workflow_tags": ["chat"],
        "target_lens": TargetLens.DEVELOPER,
        "owner_scope": "team-alpha",
        "surfaces": ModuleSurfaceContract(prompt_slots=[module_id], tools=["read"]),
        "prompt_pack_hash": f"sha256:{module_id}:prompt",
        "tool_allowlist_hash": f"sha256:{module_id}:tools",
        "hermes_version_range": ">=ee1a744",
        "privacy": PrivacyMetadata(owner_scope="team-alpha"),
        "required_adapter_capabilities": [AttachSurface.PROMPT_SLOT_INJECTION],
    }
    data.update(overrides)
    return HarnessModule.create(**data)


def _registry_with(entries):
    registry = ModuleRegistry()
    for module, layer in entries:
        registry.register(
            module,
            ModuleLifecycle.CANDIDATE,
            layer,
            consent_ok=(layer == "global"),
            redacted=(layer == "global"),
        )
    return registry


def test_resolver_manifest_hash_is_deterministic_for_reordered_inputs():
    global_mod = _module("global.mod", surfaces=ModuleSurfaceContract(prompt_slots=["global"], tools=["read", "write"]))
    tenant_mod = _module("tenant.mod", surfaces=ModuleSurfaceContract(prompt_slots=["tenant"], tools=["read"]))
    registry = _registry_with([(tenant_mod, "tenant"), (global_mod, "global")])
    resolver = CompositionResolver(registry, _contract())

    first = resolver.resolve("user", "wf", "chat", [tenant_mod.content_hash, global_mod.content_hash], set())
    second = resolver.resolve("user", "wf", "chat", [global_mod.content_hash, tenant_mod.content_hash], set())

    assert first.manifest_hash == second.manifest_hash
    assert first.model_dump() == second.model_dump()
    assert first.ordered_module_hashes == [global_mod.content_hash, tenant_mod.content_hash]


def test_conflict_rules_prompt_precedence_ui_filtering_and_deferred_disable():
    global_mod = _module(
        "global.mod",
        surfaces=ModuleSurfaceContract(
            prompt_slots=["global-prompt"],
            tools=["read", "write"],
            ui_panels=["summary:5", "missing:1"],
            safety={"allow_external": True, "max_tokens": 1000},
            budget={"allow_overrun": True, "usd": 10.0},
        ),
    )
    tenant_mod = _module(
        "tenant.mod",
        surfaces=ModuleSurfaceContract(
            prompt_slots=["tenant-prompt"],
            tools=["read", "bash"],
            ui_panels=["details:1"],
            safety={"allow_external": False, "max_tokens": 500},
            budget={"allow_overrun": False, "usd": 3.0},
        ),
    )
    user_mod = _module(
        "user.mod",
        surfaces=ModuleSurfaceContract(prompt_slots=["user-prompt"], tools=["read"], ui_panels=["summary:0"]),
    )
    deferred_mod = _module(
        "deferred.mod",
        surfaces=ModuleSurfaceContract(prompt_slots=["deferred"], tools=["read"], topology_fragment={"workers": 2}),
        required_adapter_capabilities=[AttachSurface.TOPOLOGY_SUBAGENT_CONTROL],
    )
    registry = _registry_with(
        [
            (user_mod, "user"),
            (tenant_mod, "tenant"),
            (global_mod, "global"),
            (deferred_mod, "canary"),
        ]
    )
    resolver = CompositionResolver(registry, _contract())

    manifest = resolver.resolve(
        "user",
        "wf",
        "chat",
        [user_mod.content_hash, deferred_mod.content_hash, tenant_mod.content_hash, global_mod.content_hash],
        {"summary", "details"},
        safety_floor={"max_tokens": 700},
    )

    assert manifest.resolved_tool_allowlist == ["read"]
    assert manifest.safety_policy == {"allow_external": False, "max_tokens": 500}
    assert manifest.budget_policy == {"allow_overrun": False, "usd": 3.0}
    assert manifest.resolved_prompt_order == ["global-prompt", "tenant-prompt", "user-prompt"]
    assert manifest.resolved_ui_panels == ["summary", "details", "summary"]
    assert deferred_mod.content_hash in manifest.disabled_modules
    assert global_mod.content_hash in manifest.disabled_modules
    assert any(conflict.kind == "intersection_dropped_tools" for conflict in manifest.conflicts)
    assert any(conflict.surface == "safety.allow_external" for conflict in manifest.conflicts)
    assert any(conflict.surface == "budget.allow_overrun" for conflict in manifest.conflicts)
    assert any(conflict.kind == "deferred_surface" and deferred_mod.content_hash in conflict.losers for conflict in manifest.conflicts)
    assert any(conflict.kind == "panel_not_registered" and global_mod.content_hash in conflict.losers for conflict in manifest.conflicts)
    assert manifest.manifest_hash == manifest.compute_manifest_hash()
