"""Static compatibility spike for the vendored Hermes reference."""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
from typing import Any

from ultron.hermes.capability import AdapterCapabilityContract, CapabilityStatus


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _contract_path() -> Path:
    return _repo_root() / "adapter_capability_contract.yaml"


def _symbol_exists(path: Path, symbol: str | None) -> bool:
    if not path.is_file():
        return False
    if not symbol:
        return True
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return f"def {symbol}" in text or f"class {symbol}" in text or symbol in text
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                return True
    return False


def _observe_ref(vendor_path: Path, hermes_ref: str) -> bool:
    module_path, _, symbol = hermes_ref.partition(":")
    return _symbol_exists(vendor_path / module_path, symbol or None)


def run_compatibility_spike(vendor_path: str | Path) -> dict[str, Any]:
    """Inspect vendored Hermes files without executing Hermes code."""
    vendor = Path(vendor_path)
    contract = AdapterCapabilityContract.from_yaml(_contract_path())
    generated = deepcopy(contract)
    surfaces: list[dict[str, Any]] = []

    for generated_spec in generated.surfaces:
        observations = {
            ref: _observe_ref(vendor, ref) for ref in generated_spec.hermes_refs
        }
        observed = all(observations.values()) if observations else False
        original_status = generated_spec.status
        if original_status == CapabilityStatus.SUPPORTED and not observed:
            generated_spec.status = CapabilityStatus.DEFERRED
        surfaces.append(
            {
                "surface": generated_spec.surface.value,
                "status": generated_spec.status.value,
                "asserted_status": original_status.value,
                "observed": observed,
                "hermes_refs": observations,
            }
        )

    return {
        "vendor_path": str(vendor),
        "hermes_commit": contract.hermes_commit,
        "surfaces": surfaces,
        "generated_contract": generated.model_dump(mode="json"),
    }
