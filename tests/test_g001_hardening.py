"""Hardening tests: pin consistency and self-guarding ModuleSurfaceContract."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ultron.hermes.capability import AdapterCapabilityContract
from ultron.hermes.module_surface_contract import ModuleSurfaceContract
from ultron.hermes.pin import HERMES_PINNED_COMMIT, assert_pin_matches

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "adapter_capability_contract.yaml"


def _contract() -> AdapterCapabilityContract:
    return AdapterCapabilityContract.from_yaml(CONTRACT_PATH)


def test_contract_commit_matches_pin():
    contract = _contract()
    assert contract.hermes_commit == HERMES_PINNED_COMMIT
    assert_pin_matches(contract.hermes_commit)


def test_assert_pin_matches_rejects_drift():
    with pytest.raises(ValueError):
        assert_pin_matches("deadbeef")


def test_module_contract_forbids_prohibited_keys_structurally():
    # A prohibited preserved-core key is not a declarable field -> rejected at build.
    with pytest.raises(ValidationError):
        ModuleSurfaceContract.model_validate({"global_memory_write": True})
    with pytest.raises(ValidationError):
        ModuleSurfaceContract.model_validate({"hermes_source_mutation": True})


def test_validated_rejects_deferred_surface():
    contract = _contract()
    with pytest.raises(ValueError):
        ModuleSurfaceContract.validated(
            {"topology_fragment": {"roles": ["x"]}}, contract
        )


def test_validated_accepts_clean_module():
    contract = _contract()
    inst = ModuleSurfaceContract.validated(
        {"prompt_slots": ["plan"], "tools": ["read"]}, contract
    )
    assert inst.violations(contract) == []
