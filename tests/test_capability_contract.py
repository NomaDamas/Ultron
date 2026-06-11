from pathlib import Path

import pytest

from ultron.hermes.capability import (
    AdapterCapabilityContract,
    AttachSurface,
    CapabilityStatus,
)


CONTRACT_PATH = Path(__file__).resolve().parents[1] / "adapter_capability_contract.yaml"


def test_contract_loads_validates_and_lists_all_surfaces():
    contract = AdapterCapabilityContract.from_yaml(CONTRACT_PATH)
    contract.validate()

    assert {spec.surface for spec in contract.surfaces} == set(AttachSurface)


def test_require_deferred_surface_raises():
    contract = AdapterCapabilityContract.from_yaml(CONTRACT_PATH)

    with pytest.raises(ValueError, match="deferred"):
        contract.require(AttachSurface.TOPOLOGY_SUBAGENT_CONTROL)


def test_is_supported_reflects_status():
    contract = AdapterCapabilityContract.from_yaml(CONTRACT_PATH)

    assert contract.is_supported(AttachSurface.SESSION_START) is True
    assert contract.is_supported("prompt-slot-injection") is True
    assert contract.is_supported(AttachSurface.OUTCOME_EXPORT) is False
    assert contract.get(AttachSurface.MEMORY_SKILL_ISOLATION).status == CapabilityStatus.ISOLATED_HOME_FALLBACK
