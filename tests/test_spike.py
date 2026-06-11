from pathlib import Path

import pytest

from ultron.hermes.pin import VENDOR_REF_PATH
from ultron.hermes.spike import run_compatibility_spike


def test_spike_observes_grounded_symbols_when_vendor_present():
    if not VENDOR_REF_PATH.is_dir():
        pytest.skip("vendored Hermes reference absent")

    report = run_compatibility_spike(VENDOR_REF_PATH)
    surfaces = {surface["surface"]: surface for surface in report["surfaces"]}

    expected = {
        "session-start": "agent/conversation_loop.py:run_conversation",
        "tool-toolset-allowlist": "toolsets.py:create_custom_toolset",
        "prompt-slot-injection": "agent/prompt_builder.py:build_environment_hints",
        "run-tagging": "agent/trajectory.py:save_trajectory",
        "trace-export": "agent/trajectory.py:save_trajectory",
        "budget-enforcement": "agent/iteration_budget.py:IterationBudget",
    }

    for surface, hermes_ref in expected.items():
        assert surfaces[surface]["hermes_refs"][hermes_ref] is True
        assert surfaces[surface]["observed"] is True
