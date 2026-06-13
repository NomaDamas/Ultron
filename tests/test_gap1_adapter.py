import importlib
import sys
import time
import uuid

import pytest

from ultron.app.triage import DEFAULT_SCOPE, DEFAULT_WORKFLOW, TriageApp
from ultron.composition.resolver import CompositionResolver
from ultron.evolution.variation import VariationPrimitive
from ultron.hermes.adapter import (
    AdapterRunRequest,
    AdapterRunResult,
    DeterministicFakeHermesAdapter,
    LiveHermesUnavailable,
    PinnedHermesAdapter,
)
from ultron.hermes.capability import AttachSurface, CapabilityStatus
from ultron.hermes.tool_policy import ToolPolicyCompiler
from ultron.module.model import FitnessMetadata, HarnessModule, PersistencePolicy, PrivacyMetadata, PromotionState, TargetLens
from ultron.registry.store import ModuleLifecycle
from ultron.run.manifest import RunManifest


def _request(**overrides):
    data = dict(
        run_id="run-1",
        session_id="session-1",
        user_scope="scope",
        workflow_fingerprint="workflow",
        active_module_set_id="active-1",
        active_module_set_hash="hash-1",
        ordered_module_hashes=["module-a", "module-b"],
        candidate_module_id=None,
        canary_id=None,
        persistence_mode=PersistencePolicy.ISOLATED,
        isolated_root="/tmp/iso",
        resolved_prompt_order=["triage.plan"],
        resolved_tool_allowlist=["read_file", "search_files"],
        resolved_skill_refs=["skill-a"],
        budget_policy={"max_tool_calls": 8},
        safety_policy={"workspace_writes": False},
        ui_spec_hash="ui-1",
        request_text="Fix flakes",
    )
    data.update(overrides)
    return AdapterRunRequest(**data)


def test_fake_adapter_is_deterministic_and_input_sensitive(monkeypatch):
    adapter = DeterministicFakeHermesAdapter()
    request = _request()
    first = adapter.run(request)
    monkeypatch.setattr(time, "time", lambda: 999999)
    monkeypatch.setattr(uuid, "uuid4", lambda: pytest.fail("fake adapter must not call uuid4"))
    second = adapter.run(request)
    assert first.model_dump_json() == second.model_dump_json()
    assert adapter.run(_request(run_id="run-2")).model_dump_json() != first.model_dump_json()
    assert adapter.run(_request(request_text="Different")).model_dump_json() != first.model_dump_json()


def test_tool_policy_compiler_maps_unknowns_deterministically():
    compiled = ToolPolicyCompiler.compile(["read", "search", "pytest", "bogus"])
    assert compiled.hermes_tools == ["read_file", "search_files", "terminal_process"]
    assert compiled.unknown == ["bogus"]
    assert compiled.translations == {"pytest": "terminal_process", "read": "read_file", "search": "search_files"}


class SpyAdapter:
    is_live = False
    provider_id = "spy-provider"

    def __init__(self):
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return AdapterRunResult(
            session_id=request.session_id,
            trajectory_id=f"traj-{len(self.requests)}",
            trajectory_path="spy://trajectory",
            model_provider=self.provider_id,
            model_name="spy-model",
            model_snapshot={"provider": self.provider_id, "name": "spy-model", "clean": True},
            output={"plan": [request.request_text], "risks": [], "tests": []},
            tool_calls=1,
            measured_guardrails={"ok": True},
            outcome_label="ok",
        )


def test_triage_paths_call_adapter_once_and_manifest_feedback_trace():
    adapter = SpyAdapter()
    app = TriageApp(adapter=adapter)
    run = app.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "Fix a bug")
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    manifest = run["run_manifest"]
    assert request.active_module_set_hash == manifest.active_module_set_hash
    assert request.resolved_tool_allowlist == ["read_file", "search_files", "terminal_process"]
    assert manifest.model_snapshot["provider"] == "spy-provider"
    assert "stub" not in str(manifest.model_snapshot).lower()
    feedback = app.submit_feedback(manifest.run_id)
    assert feedback.hermes_trace_id == "traj-1"

    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "candidate-spy"})
    assert len(adapter.requests) == 2
    canary_request = adapter.requests[1]
    assert canary_request.candidate_module_id == canary["candidate"].content_hash
    assert canary_request.canary_id == canary["canary_id"]
    assert canary["run_manifest"].model_snapshot["trajectory_id"] == "traj-2"


def test_pinned_adapter_plan_and_live_unavailable():
    adapter = PinnedHermesAdapter()
    request = _request(resolved_tool_allowlist=["read", "pytest"], resolved_skill_refs=["skill-a", "skill-b"])
    plan = adapter.build_invocation_plan(request)
    assert plan.request_text == request.request_text
    assert plan.prompt_slot_injections["HERMES.md"] == request.resolved_prompt_order
    assert plan.prompt_slot_injections["skills"] == ["skill-a", "skill-b"]
    assert plan.hermes_tool_allowlist == ["read_file", "terminal_process"]
    assert plan.iteration_budget["max_tool_calls"] == 8
    assert plan.isolated_home_path == "/tmp/iso/home"
    assert plan.isolated_workspace_path == "/tmp/iso/workspace"
    assert plan.trajectory_tags["run_id"] == "run-1"
    with pytest.raises(LiveHermesUnavailable):
        adapter.run(request)


def test_importing_adapter_does_not_import_vendored_hermes(monkeypatch):
    for name in list(sys.modules):
        if name == "hermes" or name.startswith("hermes."):
            del sys.modules[name]
    importlib.reload(importlib.import_module("ultron.hermes.adapter"))
    assert "hermes" not in sys.modules


def _skill_module(module_id, layer_skill_refs, *, topology=False):
    surfaces = {
        "prompt_slots": [f"{module_id}.prompt"],
        "tools": ["read", "search", "pytest"],
        "skill_refs": layer_skill_refs,
        "ui_panels": [],
        "budget": {"max_tool_calls": 8},
        "safety": {"workspace_writes": False},
    }
    if topology:
        surfaces["topology_fragment"] = {"workers": 1}
    return HarnessModule.create(
        module_id=module_id,
        name=module_id,
        version=1,
        workflow_tags=[DEFAULT_WORKFLOW],
        target_lens=TargetLens.DEVELOPER,
        owner_scope=DEFAULT_SCOPE,
        surfaces=surfaces,
        prompt_pack_hash=f"{module_id}-prompt",
        tool_allowlist_hash=f"{module_id}-tools",
        safety_policy_hash=f"{module_id}-safety",
        budget_policy_hash=f"{module_id}-budget",
        persistence_policy=PersistencePolicy.ISOLATED,
        hermes_version_range="pinned",
        privacy=PrivacyMetadata(owner_scope=DEFAULT_SCOPE, data_classes=["operational"], consent_basis="test"),
        fitness=FitnessMetadata(promotion_state=PromotionState.SEED),
    )


def test_skill_refs_end_to_end_hash_and_deferred_exclusion():
    app = TriageApp()
    core = app.registry.register(_skill_module("core", ["core-skill", "shared"]), ModuleLifecycle.SURVIVOR, "global", consent_ok=True, redacted=True)
    user = app.registry.register(_skill_module("user", ["user-skill", "shared"]), ModuleLifecycle.SURVIVOR, "user")
    deferred = app.registry.register(_skill_module("deferred", ["deferred-skill"], topology=True), ModuleLifecycle.SURVIVOR, "canary")
    manifest = app.resolver.resolve(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "triage", [core.module.content_hash, user.module.content_hash, deferred.module.content_hash], set())
    assert manifest.resolved_skill_refs == ["core-skill", "shared", "user-skill"]
    assert deferred.module.content_hash in manifest.disabled_modules
    run_manifest = RunManifest.from_manifest_set(
        manifest,
        run_id="run",
        session_id="session",
        active_module_set_id="active",
        hermes_version="h",
        adapter_version="a",
        contract_version="c",
        model_snapshot={"provider": "p", "name": "n"},
        side_effect_ledger_id="ledger",
        created_at=1.0,
        timestamp_source="test",
        persistence_mode=PersistencePolicy.ISOLATED,
    )
    assert run_manifest.resolved_skill_refs == manifest.resolved_skill_refs
    request = app._build_adapter_request(manifest, run_id="run", session_id="session", active_module_set_id="active", candidate_module_id=None, canary_id=None, persistence_mode=PersistencePolicy.ISOLATED, ui_spec_hash=None, request_text="text")
    assert request.resolved_skill_refs == manifest.resolved_skill_refs

    changed = manifest.model_copy(update={"resolved_skill_refs": manifest.resolved_skill_refs + ["extra"], "manifest_hash": None})
    assert changed.compute_manifest_hash() != manifest.manifest_hash

    deferred_contract = app.adapter_contract.model_copy(deep=True)
    specs = []
    for spec in deferred_contract.surfaces:
        if spec.surface is AttachSurface.SKILL_REFERENCE:
            specs.append(spec.model_copy(update={"status": CapabilityStatus.DEFERRED}))
        else:
            specs.append(spec)
    deferred_contract = deferred_contract.model_copy(update={"surfaces": specs})
    resolver = CompositionResolver(app.registry, deferred_contract)
    closed = resolver.resolve(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "triage", [core.module.content_hash], set())
    assert core.module.content_hash in closed.disabled_modules
