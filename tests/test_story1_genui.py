import pytest
from pydantic import ValidationError

from ultron.ui.generator import validate_generated_uispec
from ultron.ui.runtime import (
    AnimationHint,
    AnimationKind,
    COMPONENT_PROP_MODELS,
    ComponentType,
    InlineGenUiEnvelope,
    Region,
    UiComponent,
    UiSpec,
)


VALID_PROPS = {
    ComponentType.RUN_SUMMARY_CARD: {
        "run_id": "run-1",
        "workflow": "triage",
        "manifest_hash": "manifestabc",
        "trajectory_id": "traj-1",
        "status": "succeeded",
        "summary_lines": ["resolved"],
    },
    ComponentType.TOOL_RESULT_CARD: {
        "tool": "pytest",
        "status": "succeeded",
        "output_summary": ["1 passed"],
        "output_redacted": True,
        "secrets_redacted": True,
    },
    ComponentType.HARNESS_EVOLUTION_CARD: {
        "parent_hash": "abc123",
        "candidate_hash": "def456",
        "primitive": "mutation",
        "lifecycle": "candidate",
        "rationale": "Improves coverage.",
        "canary_id": "canary-1",
    },
    ComponentType.EVIDENCE_STATUS_CARD: {
        "provenance": "paired evaluation",
        "promotable": True,
        "evidence_label": "eval-1",
        "paired_tasks": 10,
    },
    ComponentType.PERSONALIZATION_SIGNAL_CARD: {
        "signal_counts": {"python": 2},
        "evidence_labels": ["label-1"],
        "summary_hash": "sum123",
        "rationale": "Bounded preference signal.",
    },
    ComponentType.SAFETY_STATUS_CARD: {
        "pending_permissions": 1,
        "rollback_state": "ready",
        "no_poisoning_ok": True,
        "gated_actions": ["RUN_BENCHMARK"],
    },
    ComponentType.ORB_STATUS: {"state": "idle", "status_text": "Ready"},
    ComponentType.TIMELINE_STEP: {"label": "Plan", "status": "complete", "detail": "Plan accepted"},
}


OVERSIZED_PROPS = {
    ComponentType.RUN_SUMMARY_CARD: {"summary_lines": ["ok"] * 9},
    ComponentType.TOOL_RESULT_CARD: {"output_summary": ["ok"] * 9},
    ComponentType.HARNESS_EVOLUTION_CARD: {"rationale": "x" * 241},
    ComponentType.EVIDENCE_STATUS_CARD: {"evidence_label": "x" * 81},
    ComponentType.PERSONALIZATION_SIGNAL_CARD: {"evidence_labels": ["ok"] * 9},
    ComponentType.SAFETY_STATUS_CARD: {"gated_actions": ["ok"] * 13},
    ComponentType.ORB_STATUS: {"status_text": "x" * 121},
    ComponentType.TIMELINE_STEP: {"detail": "x" * 181},
}


def _props(component_type):
    return dict(VALID_PROPS[component_type])


@pytest.mark.parametrize("component_type", list(VALID_PROPS))
def test_mvp_card_props_are_strict_bounded_and_accept_valid_payloads(component_type):
    model = COMPONENT_PROP_MODELS[component_type]

    assert model.model_validate(_props(component_type))

    unknown = _props(component_type)
    unknown["raw_blob"] = "not allowed"
    with pytest.raises(ValidationError):
        model.model_validate(unknown)

    oversized = _props(component_type)
    oversized.update(OVERSIZED_PROPS[component_type])
    with pytest.raises(ValidationError):
        model.model_validate(oversized)


def test_animation_hint_is_strict_bounded_and_csp_safe():
    assert AnimationHint(kind=AnimationKind.NONE, duration_ms=0, delay_ms=0)
    assert AnimationHint(kind=AnimationKind.SLIDE_UP, duration_ms=300, delay_ms=50, reduced_motion_fallback=AnimationKind.NONE)

    with pytest.raises(ValidationError):
        AnimationHint.model_validate({"kind": "spin", "duration_ms": 0, "delay_ms": 0})
    with pytest.raises(ValidationError):
        AnimationHint(kind=AnimationKind.NONE, duration_ms=1201, delay_ms=0)
    with pytest.raises(ValidationError):
        AnimationHint(kind=AnimationKind.NONE, duration_ms=0, delay_ms=1001)
    with pytest.raises(ValidationError):
        AnimationHint(kind=AnimationKind.SLIDE_UP, duration_ms=100, delay_ms=0)
    with pytest.raises(ValidationError):
        AnimationHint.model_validate({"kind": "none", "duration_ms": 0, "delay_ms": 0, "class_name": "x"})
    with pytest.raises(ValidationError):
        AnimationHint.model_validate({"kind": "none", "duration_ms": 0, "delay_ms": 0, "style": "color:red"})
    with pytest.raises(ValidationError):
        AnimationHint.model_validate({"kind": "none", "duration_ms": 0, "delay_ms": 0, "keyframes": []})


def test_uicomponent_accepts_valid_animation_and_rejects_bogus_animation_field():
    component = UiComponent(
        type=ComponentType.ORB_STATUS,
        region=Region.SIDEBAR,
        priority=0,
        props=_props(ComponentType.ORB_STATUS),
        animation={"kind": "fade_in", "duration_ms": 200, "delay_ms": 0, "reduced_motion_fallback": "none"},
    )
    assert component.animation.kind == AnimationKind.FADE_IN

    with pytest.raises(ValidationError):
        UiComponent.model_validate(
            {
                "type": "ORB_STATUS",
                "region": "sidebar",
                "priority": 0,
                "props": _props(ComponentType.ORB_STATUS),
                "animation": {"kind": "none", "duration_ms": 0, "delay_ms": 0, "selector": ".orb"},
            }
        )


def _component(component_type=ComponentType.ORB_STATUS):
    return UiComponent(type=component_type, region=Region.SIDEBAR, priority=0, props=_props(component_type))


def _panel_component(actions):
    return UiComponent(
        type=ComponentType.PLAN_PANEL,
        region=Region.MAIN,
        priority=0,
        props={"panel": "Plan", "manifest_hash": "manifestabc", "actions": actions},
    )


def _envelope(components):
    return InlineGenUiEnvelope(
        envelope_id="env-1",
        run_id="run-1",
        run_manifest_hash="manifestabc",
        manifest_signature_ok=True,
        active_module_set_hash="activeabc",
        candidate_hash="candidateabc",
        canary_id="canary-1",
        ui_spec_hash="uispecabc",
        components=components,
        provenance={"source": "runtime"},
        redaction={"secrets": True},
        created_at=1.0,
    )


def test_inline_envelope_validates_caps_registry_props_and_hashes_distinct_from_uispec():
    with pytest.raises(ValidationError):
        _envelope([_component()] * 25)

    with pytest.raises(ValueError, match="unknown component"):
        _envelope([_component(ComponentType.TIMELINE_STEP)]).finalized({ComponentType.ORB_STATUS})

    bad = _component()
    bad.props = {"state": "idle", "status_text": "x" * 121}
    with pytest.raises(ValidationError):
        _envelope([bad]).finalized({ComponentType.ORB_STATUS})

    envelope = _envelope([_component()]).finalized({ComponentType.ORB_STATUS})
    assert envelope.envelope_hash == envelope.compute_hash()
    assert envelope.envelope_hash == _envelope([_component()]).finalized({ComponentType.ORB_STATUS}).envelope_hash
    assert not isinstance(envelope, UiSpec)
    assert UiSpec(components=[_component()]).finalized({ComponentType.ORB_STATUS}).spec_hash != envelope.envelope_hash


@pytest.mark.parametrize("actions", [["RUN_BENCHMARK"], [{"type": "MODEL_DEFINED_ROOT_ACTION"}]])
def test_declared_actions_rejected_on_all_runtime_validation_paths(actions):
    component = _panel_component(actions)
    spec = UiSpec(components=[component])
    envelope = _envelope([component])

    with pytest.raises((PermissionError, ValueError)):
        spec.finalized({ComponentType.PLAN_PANEL})
    with pytest.raises((PermissionError, ValueError)):
        validate_generated_uispec(spec, {ComponentType.PLAN_PANEL})
    with pytest.raises((PermissionError, ValueError)):
        envelope.finalized({ComponentType.PLAN_PANEL})


@pytest.mark.parametrize("actions", [["GIVE_FEEDBACK"], [{"type": "GIVE_FEEDBACK"}]])
def test_known_non_privileged_declared_actions_allowed_on_all_runtime_validation_paths(actions):
    component = _panel_component(actions)
    spec = UiSpec(components=[component])
    envelope = _envelope([component])

    assert spec.finalized({ComponentType.PLAN_PANEL}).spec_hash
    assert validate_generated_uispec(spec, {ComponentType.PLAN_PANEL}).spec_hash
    assert envelope.finalized({ComponentType.PLAN_PANEL}).envelope_hash


def test_uicomponent_telemetry_schema_is_bounded():
    assert UiComponent(
        type=ComponentType.ORB_STATUS,
        region=Region.SIDEBAR,
        priority=0,
        props=_props(ComponentType.ORB_STATUS),
        telemetry_schema=["rendered", "latency_ms"],
    )

    with pytest.raises(ValidationError):
        UiComponent(
            type=ComponentType.ORB_STATUS,
            region=Region.SIDEBAR,
            priority=0,
            props=_props(ComponentType.ORB_STATUS),
            telemetry_schema=["ok"] * 9,
        )

    with pytest.raises(ValidationError):
        UiComponent(
            type=ComponentType.ORB_STATUS,
            region=Region.SIDEBAR,
            priority=0,
            props=_props(ComponentType.ORB_STATUS),
            telemetry_schema=["x" * 81],
        )


def test_validate_generated_uispec_rejects_unknown_privileged_bad_props_animation_and_count():
    with pytest.raises(ValueError, match="unknown component"):
        validate_generated_uispec(UiSpec(components=[_component(ComponentType.TIMELINE_STEP)]), {ComponentType.ORB_STATUS})

    privileged = UiSpec(
        components=[
            UiComponent(
                type=ComponentType.ORB_STATUS,
                region=Region.SIDEBAR,
                priority=0,
                props={**_props(ComponentType.ORB_STATUS), "actions": [{"type": "RUN_BENCHMARK"}]},
            )
        ]
    )
    with pytest.raises(PermissionError):
        validate_generated_uispec(privileged, {ComponentType.ORB_STATUS})

    bad_props = UiSpec(components=[UiComponent(type=ComponentType.ORB_STATUS, region=Region.SIDEBAR, priority=0, props={"state": "idle", "status_text": "x" * 121})])
    with pytest.raises(ValidationError):
        validate_generated_uispec(bad_props, {ComponentType.ORB_STATUS})

    with pytest.raises(ValidationError):
        UiSpec.model_validate(
            {
                "components": [
                    {
                        "type": "ORB_STATUS",
                        "region": "sidebar",
                        "priority": 0,
                        "props": _props(ComponentType.ORB_STATUS),
                        "animation": {"kind": "none", "duration_ms": 0, "delay_ms": 0, "style": "display:none"},
                    }
                ]
            }
        )

    with pytest.raises(ValueError, match="maximum component count"):
        validate_generated_uispec(UiSpec(components=[_component()] * 25), {ComponentType.ORB_STATUS})

    validated = validate_generated_uispec(UiSpec(components=[_component()]), {ComponentType.ORB_STATUS})
    assert validated.spec_hash == validated.compute_hash()
