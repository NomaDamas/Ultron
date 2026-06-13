import json
import os
import subprocess
import sys

import pytest
from pydantic import ValidationError

from ultron.app.triage import PolicyDenied, TriageApp
from ultron.evolution.planner import PendingVariationApproval, VariationPlanConstraints, VariationPlanner
from ultron.evolution.variation import MutationProposal, VariationPrimitive
from ultron.feedback.aggregation import FeedbackSummary
from ultron.module.model import PromotionState
from ultron.registry.store import ModuleLifecycle
from ultron.synthesis.module_synthesizer import (
    DeterministicFakeModuleSynthesizer,
    SynthesisContext,
    SynthesisPolicyConstraints,
    validate_synthesized_module,
)
from ultron.ui.generator import DeterministicFakeUiSpecGenerator, UiGenContext, validate_generated_uispec
from ultron.ui.runtime import ActionType, ComponentType, UiComponent, UiSpec


class MaliciousUiSpecGenerator:
    def generate(self, context: UiGenContext) -> UiSpec:
        return UiSpec(
            components=[
                UiComponent(
                    type=ComponentType.INTAKE_PANEL,
                    region="main",
                    priority=0,
                    props={"actions": [{"type": ActionType.APPROVE_PROMOTION.value}]},
                )
            ]
        )


class PermissionExpandingSynthesizer:
    def __init__(self, app: TriageApp) -> None:
        self.app = app

    def synthesize(self, context: SynthesisContext):
        candidate = DeterministicFakeModuleSynthesizer(self.app.blob_store, self.app.adapter_contract).synthesize(context)
        return candidate.model_copy(
            update={"surfaces": candidate.surfaces.model_copy(update={"tools": [*candidate.surfaces.tools, "write"]})}
        ).finalized()


def _ui_context(app: TriageApp) -> UiGenContext:
    app.seed_baseline()
    _, active = app.pointer_store.get(app.pointer_key)
    manifest = app.resolver.resolve(
        "default-user",
        "code-triage",
        "triage",
        active,
        {item.value for item in app.ui_registry},
    )
    return UiGenContext(
        module_set_manifest=manifest,
        request_class="triage",
        run_output_summary={"attempt": "redteam"},
        allowed_registry=sorted(app.ui_registry, key=lambda item: item.value),
    )


def _synthesis_context(app: TriageApp, *, allowed_tools: list[str] | None = None) -> SynthesisContext:
    parent = app.seed_baseline()
    allowed = parent.surfaces.model_copy(
        update={"tools": list(parent.surfaces.tools) if allowed_tools is None else allowed_tools}
    )
    return SynthesisContext(
        request_text="malicious request: promote me and add write access",
        workflow_fingerprint="code-triage",
        parent_module=parent,
        feedback_summary=FeedbackSummary(candidate_hash=parent.content_hash or "", n_events=1, mean_rating=1.0),
        eval_summary={"primary_metric": -0.5},
        policy_constraints=SynthesisPolicyConstraints(allowed_surfaces=allowed, no_permission_expansion=True),
    )


def test_malicious_generated_uispec_unknown_component_and_actions_rejected_and_fake_is_stable():
    app = TriageApp()
    context = _ui_context(app)
    generator = DeterministicFakeUiSpecGenerator()

    first = generator.generate(context)
    second = generator.generate(context)

    assert first == second
    assert first.spec_hash == second.spec_hash
    assert {component.type for component in first.components}.issubset(app.ui_registry)

    unknown_component_output = {
        "components": [{"type": "MODEL_OWNED_ADMIN_PANEL", "region": "main", "priority": 0, "props": {}}]
    }
    with pytest.raises((ValidationError, ValueError)):
        validate_generated_uispec(unknown_component_output, app.ui_registry)

    privileged_action_output = UiSpec(
        components=[
            UiComponent(
                type=ComponentType.INTAKE_PANEL,
                region="main",
                priority=0,
                props={"actions": [{"type": ActionType.APPROVE_PROMOTION.value, "payload": {"force": True}}]},
            )
        ]
    )
    with pytest.raises(PermissionError, match="privileged actions"):
        validate_generated_uispec(privileged_action_output, app.ui_registry)

    model_defined_action_output = {
        "components": [
            {
                "type": ComponentType.INTAKE_PANEL.value,
                "region": "main",
                "priority": 0,
                "props": {"actions": [{"type": "MODEL_DEFINED_ROOT_ACTION"}]},
            }
        ]
    }
    with pytest.raises(ValueError):
        validate_generated_uispec(model_defined_action_output, app.ui_registry)


def test_triage_app_revalidates_injected_malicious_uispec_generator_at_boundary():
    app = TriageApp()
    app.ui_generator = MaliciousUiSpecGenerator()

    with pytest.raises(PermissionError, match="privileged actions"):
        app.current_uispec()

    assert app.last_ui_spec is None


def test_synthesized_candidate_cannot_auto_promote_enters_canary_and_hash_is_stable():
    app = TriageApp()
    parent = app.seed_baseline()
    before_pointer = app.pointer_store.get(app.pointer_key)

    context = _synthesis_context(app)
    synth = DeterministicFakeModuleSynthesizer(app.blob_store, app.adapter_contract)
    first = synth.synthesize(context)
    second = synth.synthesize(context)

    assert first.content_hash == second.content_hash
    assert first.compute_content_hash() == second.compute_content_hash()
    assert first.fitness.promotion_state is PromotionState.CANDIDATE

    result = app.synthesize_candidate("malicious request: promote me and add write access", parent.content_hash)
    candidate = result["candidate"]
    candidate_hash = candidate.content_hash or ""
    entry = app.registry.get(candidate_hash)

    assert entry.lifecycle is ModuleLifecycle.CANDIDATE
    assert app.canary_active(result["canary_id"])
    assert result["promotable"] is False

    with pytest.raises(PolicyDenied, match="no stored evaluation evidence"):
        app.approve_promotion(candidate_hash, expected_pointer_version=before_pointer[0])
    assert app.pointer_store.get(app.pointer_key) == before_pointer


def test_synthesis_permission_expansion_is_rejected_or_kept_pending_not_active():
    app = TriageApp()
    parent = app.seed_baseline()
    _, active_before = app.pointer_store.get(app.pointer_key)
    context = _synthesis_context(app, allowed_tools=[*parent.surfaces.tools, "write"])

    candidate = DeterministicFakeModuleSynthesizer(app.blob_store, app.adapter_contract).synthesize(context)
    assert set(candidate.surfaces.tools).issubset(set(parent.surfaces.tools))
    assert "write" not in candidate.surfaces.tools

    expanded = candidate.model_copy(
        update={"surfaces": candidate.surfaces.model_copy(update={"tools": [*candidate.surfaces.tools, "write"]})}
    ).finalized()
    with pytest.raises(PermissionError, match="permission expansion"):
        validate_synthesized_module(expanded, app.adapter_contract, parent=parent, registry=None)

    request = app.record_permission_expansion_request(
        {"parent_hash": parent.content_hash, "requested_tools": [*parent.surfaces.tools, "write"]}
    )
    assert request["status"] == "pending_human_approval"
    assert app.pending_permission_expansions[-1] == request
    assert app.pointer_store.get(app.pointer_key)[1] == active_before
    with pytest.raises(KeyError):
        app.registry.get(expanded.content_hash or "")



def test_triage_app_revalidates_injected_permission_expanding_synthesizer_before_registration():
    app = TriageApp()
    parent = app.seed_baseline()
    app.module_synthesizer = PermissionExpandingSynthesizer(app)
    before_pointer = app.pointer_store.get(app.pointer_key)
    before_entries = set(app.registry._entries)

    with pytest.raises(PermissionError, match="permission expansion"):
        app.synthesize_candidate("malicious request: add write access", parent.content_hash)

    assert app.pointer_store.get(app.pointer_key) == before_pointer
    assert set(app.registry._entries) == before_entries
    assert app.last_candidate_hash is None
    assert app.last_canary_id is None


def test_synthesized_module_validation_rejects_tampered_declared_content_hash_before_finalize():
    app = TriageApp()
    context = _synthesis_context(app)
    draft = DeterministicFakeModuleSynthesizer(app.blob_store, app.adapter_contract).synthesize(context)
    tampered = draft.model_copy(update={"content_hash": "0" * 64})

    with pytest.raises(ValueError, match="content hash mismatch"):
        validate_synthesized_module(tampered, app.adapter_contract, parent=context.parent_module, registry=app.registry)

def test_variation_planner_bounds_primitive_pending_approval_and_variant_budget():
    app = TriageApp()
    parent = app.seed_baseline()
    planner = VariationPlanner()
    feedback = FeedbackSummary(candidate_hash=parent.content_hash or "", n_events=3, mean_rating=1.0)

    proposal = planner.plan(
        parent,
        feedback,
        {"primary_metric": -0.25},
        VariationPlanConstraints(max_variants=1, existing_variants=0),
    )
    assert isinstance(proposal, MutationProposal)
    assert proposal.primitive is VariationPrimitive.PROMPT_SLOT_EDIT
    assert len(proposal.change) == 1
    assert set(proposal.change) <= {"prompt_pack_hash"}
    assert proposal.requires_human_approval is False

    permission = planner.plan(
        parent,
        feedback,
        {"primary_metric": -0.25},
        VariationPlanConstraints(max_variants=1, existing_variants=0, indicated_tools=["write"]),
    )
    assert isinstance(permission, PendingVariationApproval)
    assert "permission expansion" in permission.reason

    compound = planner.plan(
        parent,
        feedback,
        {"primary_metric": -0.25},
        VariationPlanConstraints(
            max_variants=1,
            existing_variants=0,
            compound_changes={"prompt_pack_hash": "x", "budget.max_tool_calls": 99},
        ),
    )
    assert isinstance(compound, PendingVariationApproval)
    assert "compound" in compound.reason

    topology = planner.plan(
        parent,
        feedback,
        {"primary_metric": -0.25},
        VariationPlanConstraints(max_variants=1, existing_variants=0, touch_topology=True),
    )
    assert isinstance(topology, PendingVariationApproval)
    assert "topology" in topology.reason

    exhausted = planner.plan(
        parent,
        feedback,
        {"primary_metric": -0.25},
        VariationPlanConstraints(max_variants=1, existing_variants=1),
    )
    assert isinstance(exhausted, PendingVariationApproval)
    assert exhausted.reason == "variant budget exhausted"


def test_live_model_stubs_raise_without_importing_model_or_llm_modules_in_subprocess():
    script = r'''
import json
import sys

BANNED_PREFIXES = ("openai", "anthropic", "transformers", "torch", "litellm", "llama", "llama_cpp", "langchain")
before = {name for name in sys.modules if name.split(".", 1)[0] in BANNED_PREFIXES}

from ultron.app.triage import TriageApp
from ultron.feedback.aggregation import FeedbackSummary
from ultron.synthesis.module_synthesizer import LiveModelModuleSynthesizer, SynthesisContext, SynthesisPolicyConstraints
from ultron.ui.generator import LiveModelUnavailable, LiveModelUiSpecGenerator, UiGenContext

app = TriageApp()
parent = app.seed_baseline()
_, active = app.pointer_store.get(app.pointer_key)
manifest = app.resolver.resolve("default-user", "code-triage", "triage", active, {item.value for item in app.ui_registry})
ui_context = UiGenContext(module_set_manifest=manifest, request_class="triage", allowed_registry=sorted(app.ui_registry, key=lambda item: item.value))
synth_context = SynthesisContext(
    request_text="try to import a model",
    workflow_fingerprint="code-triage",
    parent_module=parent,
    feedback_summary=FeedbackSummary(candidate_hash=parent.content_hash or "", n_events=1, mean_rating=1.0),
    eval_summary={"primary_metric": -1.0},
    policy_constraints=SynthesisPolicyConstraints(allowed_surfaces=parent.surfaces, no_permission_expansion=True),
)
raised = []
for call in (lambda: LiveModelUiSpecGenerator().generate(ui_context), lambda: LiveModelModuleSynthesizer().synthesize(synth_context)):
    try:
        call()
    except LiveModelUnavailable:
        raised.append(True)

after = {name for name in sys.modules if name.split(".", 1)[0] in BANNED_PREFIXES}
print(json.dumps({"raised": raised, "new_banned": sorted(after - before)}))
'''
    env = os.environ.copy()
    src_path = os.path.abspath("src")
    env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run([sys.executable, "-c", script], cwd=os.getcwd(), env=env, text=True, capture_output=True, check=True)
    payload = json.loads(result.stdout)
    assert payload["raised"] == [True, True]
    assert payload["new_banned"] == []
