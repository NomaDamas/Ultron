import json
from pathlib import Path

from ultron.app.triage import DEFAULT_SCOPE, DEFAULT_WORKFLOW, TriageApp
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


def test_build_inline_genui_envelope_validated_bounded_and_redacted():
    app = TriageApp()
    raw_request = "Fix <script>alert('x')</script> flaky tests with a very specific raw request sentinel"
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
    assert envelope.redaction == {"request_text": True, "adapter_blob": True, "secrets": True}


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
