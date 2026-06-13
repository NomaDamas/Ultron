import pytest
from pydantic import ValidationError

from ultron.app.server import create_app
from ultron.app.triage import TriageApp
from ultron.evaluation.benchmark import BenchmarkFixture, BenchmarkRunner, BenchmarkTask, score_output, simple_adapter_request
from ultron.evaluation.harness import GuardrailMetrics
from ultron.evolution.selection import SelectionOutcome, SelectionThresholds, Selector
from ultron.evolution.variation import VariationPrimitive
from ultron.hermes.adapter import AdapterRunRequest, AdapterRunResult, DeterministicFakeHermesAdapter
from ultron.module.model import EvidenceLabel


class CountingAdapter(DeterministicFakeHermesAdapter):
    def __init__(self):
        self.requests = []

    def run(self, request: AdapterRunRequest) -> AdapterRunResult:
        self.requests.append(request)
        result = super().run(request)
        output = {"text": "focused tests"} if request.candidate_module_id else {"text": "focused"}
        return result.model_copy(update={"output": output})


class SlowCandidateAdapter(DeterministicFakeHermesAdapter):
    def run(self, request: AdapterRunRequest) -> AdapterRunResult:
        result = super().run(request)
        latency = 10.0 if request.candidate_module_id else 1.0
        tool_calls = result.tool_calls + (5 if request.candidate_module_id else 0)
        output = {"text": "focused tests"} if request.candidate_module_id else {"text": "focused"}
        return result.model_copy(update={"output": output, "tool_calls": tool_calls, "measured_guardrails": {"latency": latency, "tool_calls": tool_calls}})


def _fixture():
    return BenchmarkFixture(
        name="tiny",
        seed="fixed",
        tasks=[BenchmarkTask(task_id=f"t{i}", request_text=f"request {i}", rubric={"keywords": ["focused", "tests"], "module_hash_count_at_least": 2}) for i in range(3)],
    )


def test_eval06_non_default_thresholds_self_consistent_and_forgery_rejected():
    thresholds = SelectionThresholds(min_paired_tasks=3, min_primary_improvement=0.05)
    outcome = Selector(thresholds).evaluate("candidate", 1.0, 1.06, 4, {}, {})

    assert outcome.promotable is True
    assert outcome.evidence_label is EvidenceLabel.BENCHMARK
    assert outcome.thresholds == thresholds

    with pytest.raises(ValidationError):
        SelectionOutcome(
            candidate_hash="forged",
            evidence_label=EvidenceLabel.PREFERENCE,
            primary_delta=0.06,
            paired_tasks=4,
            guardrail_breaches=[],
            promotable=True,
            rationale="forged",
            thresholds=thresholds,
        )


def test_benchmark_runner_is_deterministic_scores_candidate_and_executes_adapter():
    fixture = _fixture()
    adapter = CountingAdapter()
    runner = BenchmarkRunner(adapter, lambda module_set, task, side: simple_adapter_request(module_set, task, side))

    first = runner.run_paired(["base"], ["base", "candidate"], fixture)
    second = runner.run_paired(["base"], ["base", "candidate"], fixture)

    assert first == second
    assert len(adapter.requests) == len(fixture.tasks) * 2 * 2
    assert all(task.candidate_metric > task.baseline_metric for task in first)
    assert score_output({"text": "focused tests"}, {"keywords": ["focused", "tests"]}) == 1.0


def test_guardrails_from_adapter_block_promotable_candidate():
    app = TriageApp(adapter=SlowCandidateAdapter())
    app.thresholds.guardrail_tolerance = {"latency": 0.0, "tool_calls": 0.0}
    app.seed_baseline()
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "slower-better"})

    decision = app.benchmark_and_decide(canary["candidate"].content_hash, _fixture(), canary["canary_id"])

    assert decision["report"].mean_primary_delta > 0
    assert decision["report"].promotable is False
    assert set(decision["report"].guardrail_breaches) >= {"latency", "tool_calls"}


def test_submit_request_does_not_create_evidence_and_benchmark_required_for_approval():
    client = TestClient(create_app())
    csrf = client.get("/").cookies["ultron_csrf"]
    submitted = client.post("/api/action", json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "triage"}})
    assert submitted.status_code == 200
    body = submitted.json()
    assert "evaluation" not in body
    candidate_hash = body["candidate"]["content_hash"]

    denied = client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={"type": "APPROVE_PROMOTION", "payload": {"candidate_hash": candidate_hash}, "csrf_token": csrf, "active_pointer_version": client.app.state.triage.current_pointer_version()},
    )
    assert denied.status_code == 403

    benchmarked = client.post("/api/action", headers={"X-CSRF-Token": csrf}, json={"type": "RUN_BENCHMARK", "payload": {"candidate_hash": candidate_hash}, "csrf_token": csrf, "active_pointer_version": client.app.state.triage.current_pointer_version()})
    assert benchmarked.status_code == 200
    assert benchmarked.json()["evaluation"]["report"]["paired_tasks"] >= 10


def test_server_has_no_fabricated_submit_metrics():
    from pathlib import Path

    server = Path("src/ultron/app/server.py").read_text()
    assert "PairedTask" not in server
    assert "candidate_metric=1.2" not in server
    assert "baseline_metric=1.0" not in server


try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover
    TestClient = None
