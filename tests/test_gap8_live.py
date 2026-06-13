import json
import os

import pytest


from ultron.app.triage import DEFAULT_SCOPE, DEFAULT_WORKFLOW, TriageApp, build_triage_app_from_env
from ultron.hermes.adapter import AdapterRunRequest, LiveHermesUnavailable, PinnedHermesAdapter
from ultron.hermes.runner import RunnerResult, SubprocessHermesRunner
from ultron.model_provider import HttpModelProvider
from ultron.module.model import PersistencePolicy
from ultron.synthesis.module_synthesizer import LiveModelModuleSynthesizer, SynthesisContext, SynthesisPolicyConstraints
from ultron.ui.generator import LiveModelUiSpecGenerator, LiveModelUnavailable, UiGenContext
from ultron.ui.runtime import ComponentType


class FakeHermesRunner:
    def __init__(self):
        self.calls = []

    def run_plan(self, plan, isolated_root):
        self.calls.append((plan, isolated_root))
        return RunnerResult(
            trajectory_id="live-traj-1",
            trajectory_path="/iso/traj.json",
            output={"plan": ["do real mapping"], "risk": [], "tests": ["pytest"]},
            tool_calls=2,
            measured_guardrails={"cost": 0, "latency_ms": 1, "tool_calls": 2},
            model_provider="openai-compatible",
            model_name="gpt-live",
            model_snapshot={"provider": "openai-compatible", "name": "gpt-live", "revision": "test"},
        )


def _request():
    return AdapterRunRequest(
        run_id="run-1",
        session_id="session-1",
        user_scope="scope",
        workflow_fingerprint="workflow",
        active_module_set_id="active-1",
        active_module_set_hash="hash-1",
        ordered_module_hashes=["module-a"],
        persistence_mode=PersistencePolicy.ISOLATED,
        isolated_root="/tmp/iso",
        resolved_prompt_order=["triage.plan"],
        resolved_tool_allowlist=["read_file"],
        resolved_skill_refs=["skill-a"],
        budget_policy={"max_tool_calls": 3},
        safety_policy={"workspace_writes": False},
        request_text="Fix live path",
    )


def test_fake_runner_drives_pinned_adapter_mapping_and_guard():
    runner = FakeHermesRunner()
    adapter = PinnedHermesAdapter(runner)
    result = adapter.run(_request())
    plan, isolated_root = runner.calls[0]
    assert isolated_root == "/tmp/iso"
    assert plan.request_text == "Fix live path"
    assert plan.hermes_tool_allowlist == ["read_file"]
    assert result.model_provider == adapter.provider_id
    assert result.model_name == "gpt-live"
    assert result.model_snapshot["runner_provider"] == "openai-compatible"
    assert result.trajectory_id == "live-traj-1"
    app = TriageApp(adapter=adapter)
    app._validate_live_adapter_result(result)


def test_pinned_adapter_without_runner_fails_closed():
    with pytest.raises(RuntimeError, match="Hermes runner"):
        PinnedHermesAdapter().run(_request())


def test_triage_app_start_run_with_fake_runner_end_to_end():
    runner = FakeHermesRunner()
    app = TriageApp(adapter=PinnedHermesAdapter(runner))
    run = app.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "Fix live adapter")
    assert run["run_manifest"].model_snapshot["provider"] == "hermes-pinned-ee1a744"
    assert runner.calls[0][1].startswith("/tmp/ultron/")


def test_subprocess_runner_without_hermes_agent_fails_closed(tmp_path):
    with pytest.raises(LiveHermesUnavailable, match="hermes-agent not installed"):
        SubprocessHermesRunner().run_plan(PinnedHermesAdapter().build_invocation_plan(_request()), str(tmp_path))


class FakeModelProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete(self, prompt, schema_hint):
        self.calls.append((prompt, schema_hint))
        return json.dumps(self.payload)


def test_live_ui_generator_parses_and_validates_fake_model():
    payload = {
        "components": [
            {"type": "PLAN_PANEL", "region": "main", "priority": 1, "props": {"actions": [{"type": "SUBMIT_REQUEST"}]}, "telemetry_schema": []}
        ]
    }
    provider = FakeModelProvider(payload)
    spec = LiveModelUiSpecGenerator(provider).generate(
        UiGenContext(module_set_manifest=object(), request_class="triage", allowed_registry=[ComponentType.PLAN_PANEL])
    )
    assert spec.spec_hash
    assert provider.calls


def test_live_ui_generator_rejects_malicious_model_output():
    unknown = {"components": [{"type": "NOT_ALLOWED", "region": "main", "priority": 1, "props": {}, "telemetry_schema": []}]}
    with pytest.raises(ValueError):
        LiveModelUiSpecGenerator(FakeModelProvider(unknown)).generate(
            UiGenContext(module_set_manifest=object(), request_class="triage", allowed_registry=[ComponentType.PLAN_PANEL])
        )
    privileged = {"components": [{"type": "PLAN_PANEL", "region": "main", "priority": 1, "props": {"actions": [{"type": "RUN_BENCHMARK"}]}, "telemetry_schema": []}]}
    with pytest.raises(PermissionError):
        LiveModelUiSpecGenerator(FakeModelProvider(privileged)).generate(
            UiGenContext(module_set_manifest=object(), request_class="triage", allowed_registry=[ComponentType.PLAN_PANEL])
        )


def test_live_module_synth_rejects_permission_expansion_and_tampered_hash():
    app = TriageApp()
    parent = app.seed_baseline()
    constraints = SynthesisPolicyConstraints(allowed_surfaces=parent.surfaces)
    context = SynthesisContext(request_text="synth", workflow_fingerprint=DEFAULT_WORKFLOW, parent_module=parent, policy_constraints=constraints)
    expanded = parent.model_copy(deep=True, update={"surfaces": parent.surfaces.model_copy(update={"tools": parent.surfaces.tools + ["write"]}), "content_hash": None}).finalized()
    with pytest.raises((PermissionError, ValueError)):
        LiveModelModuleSynthesizer(FakeModelProvider(expanded.model_dump(mode="json"))).synthesize(context)
    tampered = parent.model_copy(update={"content_hash": "tampered"})
    with pytest.raises(ValueError):
        LiveModelModuleSynthesizer(FakeModelProvider(tampered.model_dump(mode="json"))).synthesize(context)


def test_http_provider_missing_env_fails_closed(monkeypatch):
    for key in ["ULTRON_MODEL_BASE_URL", "ULTRON_MODEL_API_KEY", "ULTRON_MODEL_NAME"]:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(LiveModelUnavailable):
        HttpModelProvider().complete("prompt", None)


def test_env_config_defaults_fake_and_live_components(monkeypatch):
    monkeypatch.delenv("ULTRON_ADAPTER", raising=False)
    monkeypatch.delenv("ULTRON_UI_GENERATOR", raising=False)
    monkeypatch.delenv("ULTRON_MODULE_SYNTH", raising=False)
    app = build_triage_app_from_env()
    assert app.adapter.provider_id == "fake-deterministic"
    monkeypatch.setenv("ULTRON_ADAPTER", "pinned-hermes")
    monkeypatch.setenv("ULTRON_UI_GENERATOR", "model")
    monkeypatch.setenv("ULTRON_MODULE_SYNTH", "model")
    live = build_triage_app_from_env()
    assert live.adapter.is_live
    with pytest.raises(LiveModelUnavailable):
        live.ui_generator.generate(UiGenContext(module_set_manifest=object(), request_class="triage", allowed_registry=[ComponentType.PLAN_PANEL]))


@pytest.mark.skipif(os.getenv("ULTRON_LIVE_HERMES") != "1", reason="ULTRON_LIVE_HERMES=1 not set")
def test_env_gated_live_hermes_runner(tmp_path):
    result = SubprocessHermesRunner().run_plan(PinnedHermesAdapter().build_invocation_plan(_request()), str(tmp_path))
    assert result.trajectory_id


@pytest.mark.skipif(os.getenv("ULTRON_LIVE_MODEL") != "1", reason="ULTRON_LIVE_MODEL=1 not set")
def test_env_gated_live_model_provider():
    assert HttpModelProvider().complete('{"ping": true}', "return JSON")
