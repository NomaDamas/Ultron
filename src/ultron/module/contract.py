"""Thin integration between module declarations and Hermes capability contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ultron.hermes.capability import AdapterCapabilityContract
from ultron.hermes.module_surface_contract import ModuleSurfaceContract


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_default_contract() -> AdapterCapabilityContract:
    """Load the repo-root adapter capability contract."""
    return AdapterCapabilityContract.from_yaml(_repo_root() / "adapter_capability_contract.yaml")


def validate_declared_surfaces(
    declared: dict[str, Any], contract: AdapterCapabilityContract | None = None
) -> ModuleSurfaceContract:
    """Validate raw module surface declarations against the adapter contract."""
    return ModuleSurfaceContract.validated(declared, contract or load_default_contract())


class ModuleContract:
    """Module-facing wrapper for G001 surface validation."""

    def __init__(self, contract: AdapterCapabilityContract | None = None) -> None:
        self.contract = contract or load_default_contract()

    def validate_surfaces(self, declared: dict[str, Any]) -> ModuleSurfaceContract:
        """Return a validated surface declaration or raise on violations."""
        return ModuleSurfaceContract.validated(declared, self.contract)
