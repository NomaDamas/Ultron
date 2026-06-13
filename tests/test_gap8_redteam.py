import importlib
import json
import sys

import pytest

from ultron.app.triage import DEFAULT_SCOPE, DEFAULT_WORKFLOW, TriageApp
from ultron.evolution.variation import VariationPrimitive
from ultron.hermes.adapter import AdapterRunRequest, LiveHermesUnavailable, PinnedHermesAdapter
from ultron.hermes.runner import RunnerResult, SubprocessHermesRunner
from ultron.model_provider import HttpModelProvider
from ultron.module.model import PersistencePolicy
from ultron.synthesis.module_synthesizer import LiveModelModuleSynthesizer, SynthesisContext, SynthesisPolicyConstraints
from ultron.ui.generator import LiveModelUiSpecGenerator, LiveModelUnavailable, UiGenContext
from ultron.ui.runtime import ComponentType

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover
    TestClient = None


class FakeHermesRunner:
    def __init__(self, *, provider="openai-compatible", name="gpt-live", snapshot=None):
        self.provider = provider
        self.name = name
        self.snapshot = snapshot or {"provider": provider, "name": name, "revision": "test"}
        self.calls = []

    def run_plan(self, plan, isolated_root):
        self.calls.append((plan, isolated_root))
        issue = plan.request_text
        return RunnerResult(
            trajectory_id=f"traj-{len(self.calls)}",
            trajectory_path=f"{isolated_root}/trajectories/{len(self.calls)}.json",
            output={
                "plan": [f"Triage issue: {issue}"],
                "risk": ["stale pointer and regression risk"],
                "tests": ["pytest tests/ -q"],
                "issue_reference": issue,
                "actionable_reference": "src/ultron/app/triage.py::benchmark_and_decide",
            },
            tool_calls=2,
            measured_guardrails={"cost": 0, "latency_ms": 1, "tool_calls": 2, "external_calls": False},
            model_provider=self.provider,
            model_name=self.name,
            model_snapshot=dict(self.snapshot),
        )


class FakeModelProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete(self, prompt, schema_hint):
        self.calls.append((prompt, schema_hint))
        return json.dumps(self.payload)


class ExplodingHttpx:
    touched = False

    @staticmethod
    def post(*args, **kwargs):
        ExplodingHttpx.touched = True
        raise AssertionError("network call must not happen")


ORIGINAL_IMPORT_MODULE = importlib.import_module
class ImportBlocker:
    def __init__(self, blocked):
        self.blocked = set(blocked)
        self.calls = []

    def import_module(self, name, package=None):
        self.calls.append(name)
        if name in self.blocked:
            raise ImportError(name)
        return ORIGINAL_IMPORT_MODULE(name, package)


def _request(isolated_root="/tmp/ultron-gap8-iso"):
    return AdapterRunRequest(
        run_id="gap8-run",
        session_id="gap8-session",
        user_scope=DEFAULT_SCOPE,
        workflow_fingerprint=DEFAULT_WORKFLOW,
        active_module_set_id="active-gap8",
        active_module_set_hash="hash-gap8",
        ordered_module_hashes=["module-gap8"],
        persistence_mode=PersistencePolicy.ISOLATED,
        isolated_root=isolated_root,
        resolved_prompt_order=["triage.plan", "triage.risk"],
        resolved_tool_allowlist=["read_file", "terminal_process"],
        resolved_skill_refs=[],
        budget_policy={"max_tool_calls": 3},
        safety_policy={"workspace_writes": False, "external_calls": False},
        request_text="Fix a flaky pytest that fails only on CI",
    )


def _ui_context(app):
    app.seed_baseline()
    _, active = app.pointer_store.get(app.pointer_key)
    manifest = app.resolver.resolve(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "triage", active, {item.value for item in app.ui_registry})
    return UiGenContext(module_set_manifest=manifest, request_class="triage", allowed_registry=sorted(app.ui_registry, key=lambda item: item.value))


def _synthesis_context(app):
    parent = app.seed_baseline()
    return SynthesisContext(
        request_text="synthesize safely",
        workflow_fingerprint=DEFAULT_WORKFLOW,
        parent_module=parent,
        policy_constraints=SynthesisPolicyConstraints(allowed_surfaces=parent.surfaces, no_permission_expansion=True),
    )


def _authed(client):
    root = client.get("/")
    return root.cookies["ultron_csrf"]


def _privileged(client, csrf, action_type, payload, pointer_version=None):
    version = client.app.state.triage.current_pointer_version() if pointer_version is None else pointer_version
    return client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={"type": action_type, "payload": payload, "csrf_token": csrf, "active_pointer_version": version},
    )


def test_fail_closed_live_dependencies_and_server_503(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="Hermes runner"):
        PinnedHermesAdapter(runner=None).run(_request())

    blocker = ImportBlocker({"toolsets"})
    monkeypatch.setattr("ultron.hermes.runner.importlib.import_module", blocker.import_module)
    with pytest.raises(LiveHermesUnavailable, match="hermes-agent not installed"):
        SubprocessHermesRunner().run_plan(PinnedHermesAdapter().build_invocation_plan(_request()), str(tmp_path))
    assert "toolsets" in blocker.calls

    for key in ["ULTRON_MODEL_BASE_URL", "ULTRON_MODEL_API_KEY", "ULTRON_MODEL_NAME"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(sys.modules, "httpx", ExplodingHttpx)
    with pytest.raises(LiveModelUnavailable, match="ULTRON_MODEL_BASE_URL"):
        HttpModelProvider().complete("prompt", "schema")
    assert ExplodingHttpx.touched is False

    if TestClient is None:
        pytest.skip("fastapi test client unavailable")
    monkeypatch.setenv("ULTRON_ADAPTER", "pinned-hermes")
    monkeypatch.setenv("ULTRON_UI_GENERATOR", "fake")
    monkeypatch.setenv("ULTRON_MODULE_SYNTH", "fake")
    from ultron.app.server import create_app

    client = TestClient(create_app())
    response = client.post("/api/action", json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "live missing deps"}})
    assert response.status_code == 503
    assert "hermes-agent not installed" in response.json()["detail"]
    assert client.app.state.triage.last_manifest is None


def test_live_anti_stub_rejects_before_manifest_or_canary_side_effects():
    for runner in [
        FakeHermesRunner(provider="stub", name="gpt-live", snapshot={"provider": "stub", "name": "gpt-live"}),
        FakeHermesRunner(provider="openai-compatible", name="very-fake-model", snapshot={"provider": "openai-compatible", "name": "very-fake-model"}),
    ]:
        app = TriageApp(adapter=PinnedHermesAdapter(runner))
        app.seed_baseline()
        before_pointer = app.pointer_store.get(app.pointer_key)
        with pytest.raises(ValueError, match="stub/fake"):
            app.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "must reject")
        assert app.last_manifest is None
        assert app.pointer_store.get(app.pointer_key) == before_pointer

        before_ledger = len(app.ledger._entries)
        with pytest.raises(ValueError, match="stub/fake"):
            app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "stub candidate"})
        assert app.last_candidate_hash is None
        assert len(app.ledger._entries) == before_ledger


def test_malicious_live_model_uispec_rejected_and_not_served():
    app = TriageApp()
    unknown = {"components": [{"type": "MODEL_DEFINED_PANEL", "region": "main", "priority": 1, "props": {}, "telemetry_schema": []}]}
    with pytest.raises(ValueError):
        LiveModelUiSpecGenerator(FakeModelProvider(unknown)).generate(_ui_context(app))

    privileged = {
        "components": [
            {
                "type": "INTAKE_PANEL",
                "region": "main",
                "priority": 1,
                "props": {"actions": [{"type": "APPROVE_PROMOTION"}]},
                "telemetry_schema": [],
            }
        ]
    }
    app.ui_generator = LiveModelUiSpecGenerator(FakeModelProvider(privileged))
    with pytest.raises(PermissionError, match="privileged actions"):
        app.current_uispec()
    assert app.last_ui_spec is None


def test_malicious_live_model_module_rejected_and_not_registered_active():
    app = TriageApp()
    context = _synthesis_context(app)
    parent = context.parent_module
    _, active_before = app.pointer_store.get(app.pointer_key)

    expanded = parent.model_copy(
        deep=True,
        update={"surfaces": parent.surfaces.model_copy(update={"tools": [*parent.surfaces.tools, "write"]}), "content_hash": None},
    ).finalized()
    app.module_synthesizer = LiveModelModuleSynthesizer(FakeModelProvider(expanded.model_dump(mode="json")))
    with pytest.raises(PermissionError, match="permission expansion"):
        app.synthesize_candidate("malicious expansion")
    assert app.pointer_store.get(app.pointer_key) == (1, active_before)
    assert app.last_candidate_hash is None

    tampered = parent.model_copy(update={"content_hash": "tampered-declared-hash"})
    app.module_synthesizer = LiveModelModuleSynthesizer(FakeModelProvider(tampered.model_dump(mode="json")))
    with pytest.raises(ValueError, match="content hash mismatch"):
        app.synthesize_candidate("tampered hash")
    assert app.pointer_store.get(app.pointer_key) == (1, active_before)


def test_happy_live_fake_path_start_run_benchmark_and_promotion_evidence():
    runner = FakeHermesRunner(provider="openai-compatible", name="gpt-live")
    app = TriageApp(adapter=PinnedHermesAdapter(runner))
    app.thresholds.min_primary_improvement = 0.0
    app.thresholds.min_paired_tasks = 10

    run = app.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "Fix a flaky pytest that fails only on CI")
    assert run["run_manifest"].signature
    assert run["run_manifest"].model_snapshot["provider"] == "hermes-pinned-ee1a744"
    assert run["adapter_result"].model_snapshot["runner_provider"] == "openai-compatible"

    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "gap8 benchmark candidate"})
    candidate_hash = canary["candidate"].content_hash
    assert not app.has_promotable_evidence(candidate_hash)

    decision = app.benchmark_and_decide(candidate_hash, canary_id=canary["canary_id"])
    assert decision["report"].provenance == "benchmark_runner"
    assert decision["report"].benchmark_fixture_id == "code_triage_v0"
    assert decision["report"].benchmark_task_trajectory_ids
    assert decision["report"].promotable is True
    assert decision["report"].evidence_label == "benchmark_evidence"
    assert app.has_promotable_evidence(candidate_hash) is True
    assert len(runner.calls) >= 22


def test_model_api_key_never_appears_in_metrics_telemetry_or_live_errors(monkeypatch):
    if TestClient is None:
        pytest.skip("fastapi test client unavailable")
    secret = "gap8-secret-key-never-log"
    monkeypatch.setenv("ULTRON_MODEL_API_KEY", secret)
    monkeypatch.setenv("ULTRON_MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("ULTRON_MODEL_NAME", "gap8-live")
    monkeypatch.setenv("ULTRON_ADAPTER", "pinned-hermes")
    monkeypatch.setenv("ULTRON_UI_GENERATOR", "fake")
    monkeypatch.setenv("ULTRON_MODULE_SYNTH", "fake")

    from ultron.app.server import create_app

    client = TestClient(create_app())
    csrf = _authed(client)
    submit = client.post("/api/action", json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "key safety"}})
    metrics = client.get("/api/metrics")
    privileged = _privileged(client, csrf, "APPROVE_PROMOTION", {"candidate_hash": "missing"})

    combined = "\n".join([submit.text, metrics.text, privileged.text, json.dumps(client.app.state.triage.telemetry.snapshot(), sort_keys=True)])
    assert submit.status_code == 503
    assert metrics.status_code == 200
    assert privileged.status_code in {403, 503}
    assert secret not in combined
