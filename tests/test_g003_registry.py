from pathlib import Path

import pytest

from ultron.hermes.capability import AttachSurface
from ultron.hermes.module_surface_contract import ModuleSurfaceContract
from ultron.module.model import HarnessModule, PersistencePolicy, PrivacyMetadata, TargetLens
from ultron.registry.pointer import ActivePointerStore
from ultron.registry.store import ModuleLifecycle, ModuleRegistry


def _module(**overrides):
    data = {
        "module_id": "mod.base",
        "name": "Base",
        "version": 1,
        "parent_id": None,
        "workflow_tags": ["chat"],
        "target_lens": TargetLens.DEVELOPER,
        "owner_scope": "team-alpha",
        "surfaces": ModuleSurfaceContract(prompt_slots=["base"], tools=["read"]),
        "prompt_pack_hash": "sha256:prompt",
        "tool_allowlist_hash": "sha256:tools",
        "hermes_version_range": ">=ee1a744",
        "privacy": PrivacyMetadata(owner_scope="team-alpha"),
        "persistence_policy": PersistencePolicy.ISOLATED,
        "required_adapter_capabilities": [AttachSurface.PROMPT_SLOT_INJECTION],
    }
    data.update(overrides)
    return HarnessModule.create(**data)


def test_register_is_idempotent_for_identical_content_and_rejects_tamper_overwrite():
    registry = ModuleRegistry()
    module = _module()

    first = registry.register(module, ModuleLifecycle.SEED, "tenant")
    second = registry.register(module, ModuleLifecycle.SEED, "tenant")

    assert first is second
    assert registry.get(module.content_hash) == first

    tampered = _module(name="Tampered").model_copy(update={"content_hash": module.content_hash})
    with pytest.raises(ValueError, match="collision"):
        registry.register(tampered, ModuleLifecycle.SEED, "tenant")


def test_global_module_requires_consent_and_redaction():
    registry = ModuleRegistry()
    module = _module()

    with pytest.raises(ValueError, match="consent_ok"):
        registry.register(module, ModuleLifecycle.SEED, "global", consent_ok=True, redacted=False)

    entry = registry.register(module, ModuleLifecycle.SEED, "global", consent_ok=True, redacted=True)
    assert entry.consent_ok is True
    assert entry.redacted is True


def test_set_lifecycle_replaces_entry_without_mutating_module():
    registry = ModuleRegistry()
    module = _module()
    original = registry.register(module, ModuleLifecycle.SEED, "tenant")

    updated = registry.set_lifecycle(module.content_hash, ModuleLifecycle.SURVIVOR)

    assert original.lifecycle == ModuleLifecycle.SEED
    assert updated.lifecycle == ModuleLifecycle.SURVIVOR
    assert updated.module == original.module
    assert registry.get(module.content_hash).lifecycle == ModuleLifecycle.SURVIVOR


def test_versions_and_lineage_are_deterministic():
    registry = ModuleRegistry()
    parent = _module(module_id="mod.lineage", version=1)
    child = _module(module_id="mod.lineage", version=2, parent_id=parent.content_hash, name="Child")
    registry.register(child, ModuleLifecycle.CANDIDATE, "tenant")
    registry.register(parent, ModuleLifecycle.SEED, "tenant")

    assert [entry.module.version for entry in registry.versions_of("mod.lineage")] == [1, 2]
    assert [entry.module.content_hash for entry in registry.lineage(child.content_hash)] == [
        child.content_hash,
        parent.content_hash,
    ]


def test_can_auto_promote_blocks_permission_expansion_but_allows_refinement():
    registry = ModuleRegistry()
    parent = _module(module_id="mod.promote", version=1)
    expanding = _module(
        module_id="mod.promote",
        version=2,
        parent_id=parent.content_hash,
        surfaces=ModuleSurfaceContract(prompt_slots=["base"], tools=["read", "write"]),
    )
    refinement = _module(
        module_id="mod.promote",
        version=3,
        parent_id=parent.content_hash,
        name="Refinement",
        surfaces=ModuleSurfaceContract(prompt_slots=["refined"], tools=["read"]),
    )
    registry.register(parent, ModuleLifecycle.SEED, "tenant")
    registry.register(expanding, ModuleLifecycle.CANDIDATE, "tenant")
    registry.register(refinement, ModuleLifecycle.CANDIDATE, "tenant")

    assert registry.can_auto_promote(expanding.content_hash) is False
    assert registry.can_auto_promote(refinement.content_hash) is True


def test_active_pointer_compare_and_swap():
    store = ActivePointerStore()
    key = ("user-a", "wf")

    assert store.get(key) == (0, [])
    version = store.swap(key, 0, ["h1"])
    assert version == 1
    assert store.get(key) == (1, ["h1"])

    with pytest.raises(ValueError, match="stale"):
        store.swap(key, 0, ["h2"])

    assert store.swap(key, 1, ["h2", "h3"]) == 2
    assert store.get(key) == (2, ["h2", "h3"])
