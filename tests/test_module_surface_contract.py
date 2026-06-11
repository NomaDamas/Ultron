from pathlib import Path

from ultron.hermes.capability import AdapterCapabilityContract
from ultron.hermes.module_surface_contract import validate_module_surfaces


CONTRACT_PATH = Path(__file__).resolve().parents[1] / "adapter_capability_contract.yaml"


def _contract():
    return AdapterCapabilityContract.from_yaml(CONTRACT_PATH)


def test_rejects_topology_when_deferred():
    violations = validate_module_surfaces(
        {"topology_fragment": {"workers": 2}},
        _contract(),
    )

    assert violations
    assert violations[0].surface == "topology_fragment"
    assert "deferred" in violations[0].reason


def test_rejects_preserved_core_prohibition():
    violations = validate_module_surfaces(
        {"global_memory_write": True},
        _contract(),
    )

    assert violations
    assert violations[0].surface == "global_memory_write"
    assert "preserved-core" in violations[0].reason


def test_accepts_clean_prompt_slot_and_tool_module():
    violations = validate_module_surfaces(
        {"prompt_slots": ["HERMES.md"], "tools": ["read"]},
        _contract(),
    )

    assert violations == []
