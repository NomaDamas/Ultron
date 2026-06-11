import pytest
from pydantic import ValidationError

from ultron.composition.manifest import ModuleSetManifest
from ultron.ui.runtime import ActionCommand, ActionType, ComponentType, UiSpec, UiComponent, build_uispec_from_manifest, validate_action


def test_uispec_unknown_component_rejected_by_registry():
    spec = UiSpec(components=[UiComponent(type=ComponentType.PLAN_PANEL, region="main", priority=1)])
    with pytest.raises(ValueError):
        spec.validate({ComponentType.RISK_PANEL})


def test_action_command_extra_keys_rejected():
    with pytest.raises(ValidationError):
        ActionCommand.model_validate({"type": "SUBMIT_REQUEST", "payload": {}, "model_event": "x"})


def test_privileged_action_gates_auth_csrf_version_policy():
    cmd = ActionCommand(type=ActionType.APPROVE_PROMOTION, payload={}, csrf_token="csrf", active_pointer_version=2)
    with pytest.raises(PermissionError):
        validate_action(cmd, session_authed=False, csrf_ok=True, current_pointer_version=2, policy_ok=True)
    with pytest.raises(PermissionError):
        validate_action(cmd, session_authed=True, csrf_ok=False, current_pointer_version=2, policy_ok=True)
    with pytest.raises(PermissionError):
        validate_action(cmd, session_authed=True, csrf_ok=True, current_pointer_version=3, policy_ok=True)
    with pytest.raises(PermissionError):
        validate_action(cmd, session_authed=True, csrf_ok=True, current_pointer_version=2, policy_ok=False)
    validate_action(cmd, session_authed=True, csrf_ok=True, current_pointer_version=2, policy_ok=True)


def test_build_uispec_from_manifest_filters_to_server_registry():
    manifest = ModuleSetManifest(
        user_scope="u",
        workflow_fingerprint="wf",
        request_class="triage",
        ordered_module_hashes=["h"],
        resolved_prompt_order=[],
        resolved_tool_allowlist=[],
        resolved_ui_panels=["PLAN_PANEL:1", "RISK_PANEL:2"],
        disabled_modules=[],
        conflicts=[],
        safety_policy={},
        budget_policy={},
        rationale="test",
    ).finalized()
    spec = build_uispec_from_manifest(manifest, {ComponentType.PLAN_PANEL})
    assert [component.type for component in spec.components] == [ComponentType.PLAN_PANEL]
    assert spec.spec_hash == spec.compute_hash()
