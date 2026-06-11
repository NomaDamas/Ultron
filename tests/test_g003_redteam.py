import random
from pathlib import Path

import pytest

from ultron.composition.resolver import CompositionResolver
from ultron.hermes.capability import AdapterCapabilityContract, AttachSurface
from ultron.hermes.module_surface_contract import ModuleSurfaceContract
from ultron.module.model import HarnessModule, PersistencePolicy, PrivacyMetadata, TargetLens
from ultron.registry.pointer import ActivePointerStore
from ultron.registry.store import ModuleLifecycle, ModuleRegistry


CONTRACT_PATH = Path(__file__).resolve().parents[1] / "adapter_capability_contract.yaml"


def _contract():
    return AdapterCapabilityContract.from_yaml(CONTRACT_PATH)


def _module(module_id="mod.base", version=1, **overrides):
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
        "persistence_policy": PersistencePolicy.ISOLATED,
        "required_adapter_capabilities": [AttachSurface.PROMPT_SLOT_INJECTION],
    }
    data.update(overrides)
    return HarnessModule.create(**data)


def _register_all(registry, module_layer_pairs):
    for module, layer, *rest in module_layer_pairs:
        kwargs = rest[0] if rest else {}
        registry.register(
            module,
            ModuleLifecycle.CANDIDATE,
            layer,
            consent_ok=(layer == "global"),
            redacted=(layer == "global"),
            **kwargs,
        )
    return registry


def test_determinism_property_shuffled_inputs_registration_and_no_manifest_timestamp():
    modules = [
        (_module("global.mod", surfaces=ModuleSurfaceContract(prompt_slots=["global"], tools=["read", "write"])), "global"),
        (_module("tenant.mod", surfaces=ModuleSurfaceContract(prompt_slots=["tenant"], tools=["read", "bash"])), "tenant"),
        (_module("user.mod", surfaces=ModuleSurfaceContract(prompt_slots=["user"], tools=["read"])), "user"),
        (_module("canary.mod", surfaces=ModuleSurfaceContract(prompt_slots=["canary"], tools=["read"])), "canary"),
    ]
    rng = random.Random(1337)
    observed_hashes = set()
    observed_payloads = []

    for _ in range(40):
        registration_order = modules[:]
        rng.shuffle(registration_order)
        registry = ModuleRegistry()
        _register_all(registry, [(module, layer) for module, layer in registration_order])
        resolver = CompositionResolver(registry, _contract())

        active_hashes = [module.content_hash for module, _ in modules]
        active_hashes.extend([modules[1][0].content_hash, modules[2][0].content_hash])
        rng.shuffle(active_hashes)

        manifest = resolver.resolve("user", "wf", "chat", active_hashes, {"summary"})
        observed_hashes.add(manifest.manifest_hash)
        payload = manifest.model_dump(mode="json")
        observed_payloads.append(payload)
        payload_text = repr(payload).lower()
        assert "created_at" not in payload_text
        assert "timestamp" not in payload_text
        assert "time.time" not in payload_text

    assert len(observed_hashes) == 1
    assert all(payload == observed_payloads[0] for payload in observed_payloads)


def test_registry_immutability_idempotence_deep_copy_and_lifecycle_replacement():
    registry = ModuleRegistry()
    module = _module("immutable.mod")

    first = registry.register(module, ModuleLifecycle.SEED, "tenant")
    second = registry.register(module, ModuleLifecycle.SEED, "tenant")
    assert first is second

    stored_before = registry.get(module.content_hash)
    returned_entry = registry.get(module.content_hash)
    returned_entry.lifecycle = ModuleLifecycle.QUARANTINED
    returned_entry.module.name = "mutated name"
    returned_entry.module.surfaces.tools.append("write")

    stored_after = registry.get(module.content_hash)
    assert stored_after.lifecycle == stored_before.lifecycle == ModuleLifecycle.SEED
    assert stored_after.module.name == "immutable.mod"
    assert stored_after.module.surfaces.tools == ["read"]

    updated = registry.set_lifecycle(module.content_hash, ModuleLifecycle.SURVIVOR)
    assert updated.lifecycle == ModuleLifecycle.SURVIVOR
    assert updated.module.name == "immutable.mod"
    assert updated.module.surfaces.tools == ["read"]
    assert stored_before.lifecycle == ModuleLifecycle.SEED
    assert stored_before.module.name == "immutable.mod"
    assert stored_before.module.surfaces.tools == ["read"]


def test_global_gating_requires_consent_and_redaction_then_accepts():
    registry = ModuleRegistry()
    module = _module("global.gated")

    with pytest.raises(ValueError, match="consent_ok"):
        registry.register(module, ModuleLifecycle.SEED, "global")
    with pytest.raises(ValueError, match="consent_ok"):
        registry.register(module, ModuleLifecycle.SEED, "global", consent_ok=True)

    entry = registry.register(module, ModuleLifecycle.SEED, "global", consent_ok=True, redacted=True)
    assert entry.layer == "global"
    assert entry.consent_ok is True
    assert entry.redacted is True


def test_can_auto_promote_blocks_permission_expansions_and_allows_refinement():
    registry = ModuleRegistry()
    parent = _module(
        "promote.parent",
        surfaces=ModuleSurfaceContract(prompt_slots=["base"], tools=["read"], persistence={"mode": "isolated"}),
        persistence_policy=PersistencePolicy.ISOLATED,
        required_adapter_capabilities=[AttachSurface.PROMPT_SLOT_INJECTION],
    )
    variants = {
        "tool": _module(
            "promote.tool",
            parent_id=parent.content_hash,
            surfaces=ModuleSurfaceContract(prompt_slots=["base"], tools=["read", "write"], persistence={"mode": "isolated"}),
        ),
        "surface": _module(
            "promote.surface",
            parent_id=parent.content_hash,
            surfaces=ModuleSurfaceContract(prompt_slots=["base"], tools=["read"], ui_panels=["summary:1"], persistence={"mode": "isolated"}),
        ),
        "capability": _module(
            "promote.capability",
            parent_id=parent.content_hash,
            surfaces=ModuleSurfaceContract(prompt_slots=["base"], tools=["read"], persistence={"mode": "isolated"}),
            required_adapter_capabilities=[AttachSurface.PROMPT_SLOT_INJECTION, AttachSurface.TOOL_TOOLSET_ALLOWLIST],
        ),
        "persistence": _module(
            "promote.persistence",
            parent_id=parent.content_hash,
            surfaces=ModuleSurfaceContract(prompt_slots=["base"], tools=["read"], persistence={"mode": "isolated"}),
            persistence_policy=PersistencePolicy.NORMAL,
        ),
        "refinement": _module(
            "promote.refinement",
            name="prompt refinement only",
            parent_id=parent.content_hash,
            surfaces=ModuleSurfaceContract(prompt_slots=["refined prompt"], tools=["read"], persistence={"mode": "isolated"}),
        ),
    }
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    for child in variants.values():
        registry.register(child, ModuleLifecycle.CANDIDATE, "tenant")

    assert registry.can_auto_promote(variants["tool"].content_hash) is False
    assert registry.can_auto_promote(variants["surface"].content_hash) is False
    assert registry.can_auto_promote(variants["capability"].content_hash) is False
    assert registry.can_auto_promote(variants["persistence"].content_hash) is False
    assert registry.can_auto_promote(variants["refinement"].content_hash) is True


def test_conflict_rules_restrictive_intersection_additive_prompts_ui_and_deferred():
    core_mod = _module(
        "core.mod",
        surfaces=ModuleSurfaceContract(prompt_slots=["core"], tools=["read", "write", "bash"], safety={"allow_external": True, "max_tokens": 2000}, budget={"allow_overrun": True, "usd": 20.0}),
    )
    global_mod = _module(
        "global.mod",
        surfaces=ModuleSurfaceContract(prompt_slots=["global"], tools=["read", "write"], ui_panels=["missing:1"], safety={"allow_external": True, "max_tokens": 1000}, budget={"allow_overrun": True, "usd": 10.0}),
    )
    tenant_mod = _module(
        "tenant.mod",
        surfaces=ModuleSurfaceContract(prompt_slots=["tenant"], tools=["read", "bash"], safety={"allow_external": False, "max_tokens": 500}, budget={"allow_overrun": False, "usd": 3.0}),
    )
    user_mod = _module("user.mod", surfaces=ModuleSurfaceContract(prompt_slots=["user"], tools=["read"], ui_panels=["summary:3"]))
    canary_mod = _module("canary.mod", surfaces=ModuleSurfaceContract(prompt_slots=["canary"], tools=["read"], ui_panels=["details:2"]))
    deferred_mod = _module(
        "deferred.mod",
        surfaces=ModuleSurfaceContract(prompt_slots=["deferred"], tools=["read"], topology_fragment={"workers": 1}),
        required_adapter_capabilities=[AttachSurface.TOPOLOGY_SUBAGENT_CONTROL],
    )

    registry = _register_all(
        ModuleRegistry(),
        [
            (canary_mod, "canary"),
            (user_mod, "user"),
            (tenant_mod, "tenant"),
            (global_mod, "global"),
            (core_mod, "global"),
            (deferred_mod, "canary"),
        ],
    )
    resolver = CompositionResolver(registry, _contract())
    active = [deferred_mod.content_hash, user_mod.content_hash, tenant_mod.content_hash, global_mod.content_hash, core_mod.content_hash, canary_mod.content_hash]
    manifest = resolver.resolve("user", "wf", "chat", active, {"summary", "details"})

    assert manifest.safety_policy["max_tokens"] == 500
    assert manifest.budget_policy["usd"] == 3.0
    assert manifest.safety_policy["allow_external"] is False
    assert manifest.budget_policy["allow_overrun"] is False
    assert manifest.resolved_tool_allowlist == ["read"]
    assert any(conflict.kind == "intersection_dropped_tools" and "bash" in conflict.rationale and "write" in conflict.rationale for conflict in manifest.conflicts)
    assert manifest.resolved_prompt_order == ["core", "global", "tenant", "user", "canary"]
    assert manifest.resolved_ui_panels == ["details", "summary"]
    assert global_mod.content_hash in manifest.disabled_modules
    assert any(conflict.kind == "panel_not_registered" and global_mod.content_hash in conflict.losers for conflict in manifest.conflicts)
    assert deferred_mod.content_hash in manifest.disabled_modules
    assert any(conflict.kind == "deferred_surface" and deferred_mod.content_hash in conflict.losers and "topology" in conflict.rationale for conflict in manifest.conflicts)

    additive_registry = _register_all(
        ModuleRegistry(),
        [
            (tenant_mod, "tenant", {"human_approved_additive": True}),
            (global_mod, "global"),
        ],
    )
    additive_manifest = CompositionResolver(additive_registry, _contract()).resolve(
        "user", "wf", "chat", [tenant_mod.content_hash, global_mod.content_hash], set()
    )
    assert additive_manifest.resolved_tool_allowlist == ["bash", "read"]
    assert any(conflict.kind == "approved_additive_tools" and "bash" in conflict.rationale for conflict in additive_manifest.conflicts)
    assert any(conflict.kind == "intersection_dropped_tools" and "write" in conflict.rationale for conflict in additive_manifest.conflicts)


def test_active_pointer_compare_and_swap_rejects_stale_and_preserves_state():
    store = ActivePointerStore()
    key = ("user-a", "workflow-a")

    assert store.get(key) == (0, [])
    assert store.swap(key, 0, ["h1"]) == 1
    assert store.get(key) == (1, ["h1"])

    with pytest.raises(ValueError, match="stale"):
        store.swap(key, 0, ["evil"])
    assert store.get(key) == (1, ["h1"])

    assert store.swap(key, 1, ["h2", "h3"]) == 2
    assert store.get(key) == (2, ["h2", "h3"])
