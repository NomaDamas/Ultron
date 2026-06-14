import pytest

from ultron.app.triage import TriageApp
from ultron.evolution.planner import PendingVariationApproval, VariationPlanConstraints, VariationPlanner
from ultron.evolution.variation import MutationProposal, VariationPrimitive
from ultron.feedback.aggregation import FeedbackSummary
from ultron.module.blobs import BlobKind
from ultron.module.model import PromotionState
from ultron.registry.store import ModuleLifecycle
from ultron.synthesis.module_synthesizer import (
    DeterministicFakeModuleSynthesizer,
    LiveModelModuleSynthesizer,
    SynthesisContext,
    SynthesisPolicyConstraints,
    validate_synthesized_module,
)
from ultron.ui.generator import (
    DeterministicFakeUiSpecGenerator,
    LiveModelUnavailable,
    LiveModelUiSpecGenerator,
    UiGenContext,
    validate_generated_uispec,
)
from ultron.ui.runtime import ActionType, ComponentType, UiComponent, UiSpec


def _ui_context(app):
    app.seed_baseline()
    _, active = app.pointer_store.get(app.pointer_key)
    manifest = app.resolver.resolve("default-user", "code-triage", "triage", active, {item.value for item in app.ui_registry})
    return UiGenContext(module_set_manifest=manifest, request_class="triage", run_output_summary={}, allowed_registry=sorted(app.ui_registry, key=lambda item: item.value))


def test_uispec_fake_validates_deterministic_and_rejects_generated_privileged_action():
    app = TriageApp()
    context = _ui_context(app)
    generator = DeterministicFakeUiSpecGenerator()

    first = generator.generate(context)
    second = generator.generate(context)

    assert first == second
    assert first.spec_hash
    assert {component.type for component in first.components}.issubset(app.ui_registry)

    malicious = UiSpec(
        components=[
            UiComponent(
                type=ComponentType.INTAKE_PANEL,
                region="main",
                priority=0,
                props={"actions": [{"type": ActionType.APPROVE_PROMOTION.value}]},
            )
        ]
    )
    with pytest.raises(PermissionError):
        validate_generated_uispec(malicious, app.ui_registry)


def test_generated_uispec_unknown_component_is_rejected_by_server_registry():
    malicious = UiSpec(components=[UiComponent(type=ComponentType.TRACE_PANEL, region="details", priority=0)])

    with pytest.raises(ValueError, match="unknown component"):
        validate_generated_uispec(malicious, {ComponentType.INTAKE_PANEL})


def test_generated_uispec_region_is_constrained_by_server_validation():
    app = TriageApp()
    safe = UiSpec(components=[UiComponent(type=ComponentType.INTAKE_PANEL, region="sidebar", priority=0)])

    validated = validate_generated_uispec(safe, app.ui_registry)

    assert validated.components[0].region == "sidebar"
    with pytest.raises(ValueError):
        validate_generated_uispec(
            {"components": [{"type": "INTAKE_PANEL", "region": "main\"] .bad", "priority": 0}]},
            app.ui_registry,
        )

def _synthesis_context(app, *, allowed_tools=None):
    parent = app.seed_baseline()
    allowed = parent.surfaces.model_copy(update={"tools": allowed_tools if allowed_tools is not None else list(parent.surfaces.tools)})
    return SynthesisContext(
        request_text="prefer focused regression tests",
        workflow_fingerprint="code-triage",
        parent_module=parent,
        feedback_summary=FeedbackSummary(candidate_hash=parent.content_hash or "", n_events=1, mean_rating=2.0),
        eval_summary={"primary_metric": -0.1},
        policy_constraints=SynthesisPolicyConstraints(allowed_surfaces=allowed, no_permission_expansion=True),
    )


def test_fake_module_synthesizer_deterministic_real_blobs_and_registered_candidate_not_promotable():
    app = TriageApp()
    context = _synthesis_context(app)
    synth = DeterministicFakeModuleSynthesizer(app.blob_store, app.adapter_contract)

    first = synth.synthesize(context)
    second = synth.synthesize(context)

    assert first.content_hash == second.content_hash
    assert first.parent_id == context.parent_module.content_hash
    assert set(first.surfaces.tools).issubset(set(context.parent_module.surfaces.tools))
    assert first.fitness.promotion_state is PromotionState.CANDIDATE
    for kind, content_hash in first.referenced_blob_hashes().items():
        if kind in {BlobKind.PROMPT_PACK, BlobKind.TOOL_POLICY, BlobKind.UI_PANEL_CONTRACT, BlobKind.SAFETY_POLICY, BlobKind.BUDGET_POLICY}:
            assert content_hash and app.blob_store.has(kind, content_hash)

    result = app.synthesize_candidate("prefer focused regression tests", context.parent_module.content_hash)
    entry = app.registry.get(result["candidate"].content_hash or "")
    assert entry.lifecycle is ModuleLifecycle.CANDIDATE
    assert app.canary_active(result["canary_id"])
    assert result["promotable"] is False
    assert app.has_promotable_evidence(result["candidate"].content_hash or "") is False


def test_synthesized_permission_expansion_rejected_by_server_validation():
    app = TriageApp()
    context = _synthesis_context(app)
    candidate = DeterministicFakeModuleSynthesizer(app.blob_store, app.adapter_contract).synthesize(context)
    expanded = candidate.model_copy(update={"surfaces": candidate.surfaces.model_copy(update={"tools": list(candidate.surfaces.tools) + ["write"]})}).finalized()

    with pytest.raises(PermissionError, match="permission expansion"):
        validate_synthesized_module(expanded, app.adapter_contract, parent=context.parent_module, registry=None)


def test_live_model_seams_fail_closed_without_model_import_and_revalidate_malicious_outputs():
    app = TriageApp()
    with pytest.raises(LiveModelUnavailable):
        LiveModelUiSpecGenerator().generate(_ui_context(app))
    with pytest.raises(LiveModelUnavailable):
        LiveModelModuleSynthesizer().synthesize(_synthesis_context(app))

    privileged = UiSpec(
        components=[UiComponent(type=ComponentType.INTAKE_PANEL, region="main", priority=0, props={"actions": [ActionType.RESTORE_MODULE.value]})]
    )
    with pytest.raises(PermissionError):
        validate_generated_uispec(privileged, app.ui_registry)

    context = _synthesis_context(app)
    draft = DeterministicFakeModuleSynthesizer(app.blob_store, app.adapter_contract).synthesize(context)
    expanded = draft.model_copy(update={"surfaces": draft.surfaces.model_copy(update={"tools": [*draft.surfaces.tools, "write"]})}).finalized()
    with pytest.raises(PermissionError):
        validate_synthesized_module(expanded, app.adapter_contract, parent=context.parent_module, registry=None)


def test_variation_planner_one_primitive_pending_approval_and_budget():
    app = TriageApp()
    parent = app.seed_baseline()
    planner = VariationPlanner()
    feedback = FeedbackSummary(candidate_hash=parent.content_hash or "", n_events=2, mean_rating=2.0)

    proposal = planner.plan(parent, feedback, {"primary_metric": -0.2}, VariationPlanConstraints(max_variants=1, existing_variants=0))
    assert isinstance(proposal, MutationProposal)
    assert proposal.primitive is VariationPrimitive.PROMPT_SLOT_EDIT
    assert len(proposal.change) == 1
    assert proposal.requires_human_approval is False

    pending = planner.plan(parent, feedback, {"primary_metric": -0.2}, VariationPlanConstraints(max_variants=1, existing_variants=0, indicated_tools=["write"]))
    assert isinstance(pending, PendingVariationApproval)
    assert "permission expansion" in pending.reason

    exhausted = planner.plan(parent, feedback, {"primary_metric": -0.2}, VariationPlanConstraints(max_variants=1, existing_variants=1))
    assert isinstance(exhausted, PendingVariationApproval)
    assert exhausted.reason == "variant budget exhausted"
