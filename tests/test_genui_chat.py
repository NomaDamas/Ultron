import json
from pathlib import Path

from ultron.app.triage import DEFAULT_SCOPE, DEFAULT_WORKFLOW, TriageApp
from ultron.hermes.adapter import AdapterRunRequest, AdapterRunResult
from ultron.evolution.variation import VariationPrimitive
from ultron.ui.runtime import COMPONENT_PROP_MODELS, ComponentType, InlineGenUiEnvelope, UiComponent, validate_component_props, validate_inline_envelope


MVP_TYPES = {
    "RUN_SUMMARY_CARD",
    "TOOL_RESULT_CARD",
    "HARNESS_EVOLUTION_CARD",
    "EVIDENCE_STATUS_CARD",
    "PERSONALIZATION_SIGNAL_CARD",
    "SAFETY_STATUS_CARD",
    "ORB_STATUS",
    "TIMELINE_STEP",
}



class PoisonedScalarAdapter:
    @property
    def is_live(self) -> bool:
        return False

    @property
    def provider_id(self) -> str:
        return "poisoned-provider"

    def run(self, request: AdapterRunRequest) -> AdapterRunResult:
        secret_scalar = "ghp_FAKESECRET123456-sk-FAKESECRET123456-user@example.com-0123456789abcdef0123456789abcdef"
        return AdapterRunResult(
            session_id=request.session_id,
            trajectory_id=f"traj-{secret_scalar}",
            trajectory_path="fake://poisoned",
            model_provider="poisoned-provider",
            model_name=f"model-{secret_scalar}",
            model_snapshot={"provider": "poisoned-provider", "name": f"model-{secret_scalar}"},
            output={"plan": ["safe plan"], "risk": ["safe risk"], "tests": ["safe tests"]},
            tool_calls=1,
            measured_guardrails={"cost": 0},
            outcome_label="succeeded",
        )


def test_build_inline_genui_envelope_validated_bounded_and_redacted():
    app = TriageApp()
    raw_request = "Fix ghp_FAKESECRET123456 sk-FAKESECRET123456 user@example.com flaky tests with a very specific raw request sentinel"
    run = app.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, raw_request)
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "candidate-good"}, raw_request)

    envelope = app.build_inline_genui_envelope(run, canary)
    validate_inline_envelope(envelope, app.ui_registry)
    body = envelope.model_dump(mode="json")
    encoded = json.dumps(body, sort_keys=True)

    assert envelope.envelope_hash
    assert envelope.manifest_signature_ok is True
    assert len(envelope.components) <= 24
    for component in envelope.components:
        assert component.type in app.ui_registry
        validate_component_props(component)
    assert raw_request not in encoded
    assert "raw request sentinel" not in encoded
    assert run["adapter_result"].model_dump_json() not in encoded
    assert "ghp_FAKESECRET123456" not in encoded
    assert "sk-FAKESECRET123456" not in encoded
    assert "user@example.com" not in encoded
    assert "[redacted]" in encoded
    assert envelope.redaction == {"request_text": True, "adapter_blob": True, "secrets": True, "applied": True}



def test_poisoned_adapter_scalar_props_are_redacted_and_bounded():
    app = TriageApp(adapter=PoisonedScalarAdapter())
    raw_request = "Fix the poisoned adapter scalar leak"
    run = app.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, raw_request)
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "candidate-good"}, raw_request)

    envelope = app.build_inline_genui_envelope(run, canary)
    validate_inline_envelope(envelope, app.ui_registry)
    encoded = json.dumps(envelope.model_dump(mode="json"), sort_keys=True)

    for leaked in (
        "ghp_FAKESECRET123456",
        "sk-FAKESECRET123456",
        "user@example.com",
        "0123456789abcdef0123456789abcdef",
        run["adapter_result"].trajectory_id,
        run["adapter_result"].model_name,
    ):
        assert leaked not in encoded
    run_summary = next(component for component in envelope.components if component.type == ComponentType.RUN_SUMMARY_CARD)
    assert run_summary.props["trajectory_id"] != run["adapter_result"].trajectory_id
    assert len(run_summary.props["trajectory_id"]) <= 80
    assert "[redacted]" in encoded

def test_malicious_request_not_rendered_as_html_props_and_envelope_validates():
    app = TriageApp()
    raw_request = "<script>alert(1)</script> \"quoted\" request"
    run = app.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, raw_request)
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "candidate-good"}, raw_request)
    envelope = app.build_inline_genui_envelope(run, canary)

    validate_inline_envelope(envelope, app.ui_registry)
    encoded = json.dumps(envelope.model_dump(mode="json"), sort_keys=True)
    assert raw_request not in encoded
    assert "<script>" not in encoded
    assert "</script>" not in encoded


def test_chat_js_renderer_parity_textcontent_only_and_animation_allowlist():
    source = Path("src/ultron/app/static/chat.js").read_text()
    for component_type in MVP_TYPES:
        assert f"{component_type}:" in source
    assert ".textContent" in source
    assert "ANIMATION_CLASS[kind]" in source
    assert "innerHTML" not in source
    assert "eval(" not in source
    assert "new Function" not in source
    assert ".style" not in source
    assert "withActivePointerVersion" in source
    assert "active_pointer_version" in source
