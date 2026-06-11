from pathlib import Path
import sys

import pytest
import yaml
from pydantic import ValidationError

from ultron.hermes.capability import (
    AdapterCapabilityContract,
    AttachSurface,
    CapabilityStatus,
)
from ultron.hermes.module_surface_contract import validate_module_surfaces
from ultron.hermes.pin import VENDOR_REF_PATH
from ultron.hermes.spike import run_compatibility_spike


CONTRACT_PATH = Path(__file__).resolve().parents[1] / "adapter_capability_contract.yaml"


def _contract() -> AdapterCapabilityContract:
    return AdapterCapabilityContract.from_yaml(CONTRACT_PATH)


def _contract_data() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_g001_redteam_contract_rejects_missing_required_surface():
    data = _contract_data()
    removed = data["surfaces"].pop()

    with pytest.raises(ValidationError) as excinfo:
        AdapterCapabilityContract.model_validate(data)

    assert "missing attach surfaces" in str(excinfo.value)
    assert removed["surface"] in str(excinfo.value)


def test_g001_redteam_contract_rejects_duplicate_surface():
    data = _contract_data()
    data["surfaces"].append(dict(data["surfaces"][0]))

    with pytest.raises(ValidationError) as excinfo:
        AdapterCapabilityContract.model_validate(data)

    assert "duplicate attach surfaces" in str(excinfo.value)
    assert data["surfaces"][0]["surface"] in str(excinfo.value)


def test_g001_redteam_contract_rejects_invalid_status_string():
    data = _contract_data()
    data["surfaces"][0]["status"] = "MAYBE_SUPPORTED"

    with pytest.raises(ValidationError) as excinfo:
        AdapterCapabilityContract.model_validate(data)

    assert "MAYBE_SUPPORTED" in str(excinfo.value)


@pytest.mark.parametrize("surface", [surface for surface in AttachSurface])
def test_g001_redteam_require_and_is_supported_match_status(surface):
    contract = _contract()
    spec = contract.get(surface)

    assert contract.is_supported(surface) is (spec.status == CapabilityStatus.SUPPORTED)
    if spec.status == CapabilityStatus.DEFERRED:
        with pytest.raises(ValueError, match="deferred"):
            contract.require(surface)
    else:
        assert contract.require(surface) == spec
        if spec.status in {
            CapabilityStatus.PARTIAL,
            CapabilityStatus.ISOLATED_HOME_FALLBACK,
        }:
            assert contract.is_supported(surface) is False


def test_g001_redteam_round_trip_yaml_preserves_contract_semantics(tmp_path):
    original = _contract()
    round_trip_path = tmp_path / "contract.yaml"
    round_trip_path.write_text(original.to_yaml(), encoding="utf-8")

    reparsed = AdapterCapabilityContract.from_yaml(round_trip_path)

    assert reparsed.model_dump(mode="json") == original.model_dump(mode="json")


def test_g001_redteam_module_rejects_deferred_attach_surface():
    violations = validate_module_surfaces(
        {"topology_fragment": {"workers": 2}},
        _contract(),
    )

    assert [(violation.surface, violation.reason) for violation in violations] == [
        (
            "topology_fragment",
            "attach surface is deferred: topology-subagent-control",
        )
    ]


@pytest.mark.parametrize(
    "prohibited_surface",
    ["global_memory_write", "hermes_source_mutation", "credential_mutation"],
)
def test_g001_redteam_module_rejects_preserved_core_prohibitions(prohibited_surface):
    violations = validate_module_surfaces({prohibited_surface: True}, _contract())

    assert violations
    assert violations[0].surface == prohibited_surface
    assert "preserved-core" in violations[0].reason


def test_g001_redteam_module_accepts_clean_prompt_slot_and_tool_module():
    violations = validate_module_surfaces(
        {"prompt_slots": ["HERMES.md"], "tools": ["read", "write"]},
        _contract(),
    )

    assert violations == []


def test_g001_redteam_spike_observes_vendor_without_import_execution(monkeypatch):
    if not VENDOR_REF_PATH.is_dir():
        pytest.skip("vendored Hermes reference absent")

    before_modules = set(sys.modules)
    report = run_compatibility_spike(VENDOR_REF_PATH)
    after_modules = set(sys.modules)

    assert report["surfaces"]
    assert any(surface["hermes_refs"] for surface in report["surfaces"])
    assert not any(
        module not in before_modules and (module == "agent" or module.startswith("agent."))
        for module in after_modules
    )
    assert "hermes_state" not in (after_modules - before_modules)


def test_g001_redteam_spike_detects_symbols_by_ast_or_text_without_import(tmp_path, monkeypatch):
    vendor = tmp_path / "vendor"
    (vendor / "agent").mkdir(parents=True)
    (vendor / "agent" / "conversation_loop.py").write_text(
        "raise RuntimeError('import execution would be a contract violation')\n"
        "def run_conversation():\n"
        "    return 'detected by ast, not import'\n",
        encoding="utf-8",
    )
    (vendor / "agent" / "prompt_builder.py").write_text(
        "def build_environment_hints():\n"
        "    pass\n"
        "def _find_hermes_md():\n"
        "    pass\n"
        "def load_soul_md():\n"
        "    pass\n"
        "def build_skills_system_prompt():\n"
        "    pass\n"
        "def _build_skills_manifest():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (vendor / "agent" / "system_prompt.py").write_text("SYSTEM = 'ok'\n", encoding="utf-8")
    (vendor / "toolsets.py").write_text(
        "def create_custom_toolset():\n"
        "    pass\n"
        "def resolve_multiple_toolsets():\n"
        "    pass\n"
        "def validate_toolset():\n"
        "    pass\n"
        "def get_toolset_names():\n"
        "    pass\n"
        "def get_all_toolsets():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (vendor / "agent" / "skill_bundles.py").write_text("SKILLS = []\n", encoding="utf-8")
    (vendor / "agent" / "trajectory.py").write_text(
        "def save_trajectory():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (vendor / "agent" / "iteration_budget.py").write_text(
        "class IterationBudget:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (vendor / "hermes_state.py").write_text(
        "this is invalid python, but text contains class SessionDB for fallback detection",
        encoding="utf-8",
    )

    def fail_import(name, *args, **kwargs):
        if name in {"hermes_state", "toolsets"} or name == "agent" or name.startswith("agent."):
            raise AssertionError(f"run_compatibility_spike imported Hermes module {name}")
        return original_import(name, *args, **kwargs)

    original_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
    monkeypatch.setattr("builtins.__import__", fail_import)

    report = run_compatibility_spike(vendor)
    surfaces = {surface["surface"]: surface for surface in report["surfaces"]}

    assert surfaces["session-start"]["hermes_refs"]["agent/conversation_loop.py:run_conversation"] is True
    assert surfaces["session-start"]["hermes_refs"]["hermes_state.py:SessionDB"] is True
    assert surfaces["prompt-slot-injection"]["observed"] is True
    assert surfaces["tool-toolset-allowlist"]["observed"] is True
    assert surfaces["budget-enforcement"]["hermes_refs"]["agent/iteration_budget.py:IterationBudget"] is True
