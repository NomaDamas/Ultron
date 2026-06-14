from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import ultron.evaluation.benchmark as benchmark_module
import ultron.app.triage as triage_module
from ultron.app.server import create_app
from ultron.app.triage import TriageApp
from ultron.evaluation.benchmark import BenchmarkFixture, BenchmarkRunner, BenchmarkTask, simple_adapter_request
from ultron.evaluation.harness import GuardrailMetrics, PairedTask
from ultron.evolution.selection import SelectionOutcome, SelectionThresholds, Selector
from ultron.evolution.variation import VariationPrimitive
from ultron.hermes.adapter import AdapterRunRequest, AdapterRunResult, DeterministicFakeHermesAdapter
from ultron.module.model import EvidenceLabel


class SpyAdapter(DeterministicFakeHermesAdapter):
    def __init__(self) -> None:
        self.requests: list[AdapterRunRequest] = []

    def run(self, request: AdapterRunRequest) -> AdapterRunResult:
        self.requests.append(request)
        result = super().run(request)
        output = {"text": "focused tests"} if request.candidate_module_id else {"text": "focused"}
        return result.model_copy(update={"output": output})


class RegressingGuardrailAdapter(DeterministicFakeHermesAdapter):
    def run(self, request: AdapterRunRequest) -> AdapterRunResult:
        result = super().run(request)
        is_candidate = request.candidate_module_id is not None
        output = {"text": "focused tests", "module_hashes": request.ordered_module_hashes} if is_candidate else {"text": "focused", "module_hashes": request.ordered_module_hashes[:1]}
        tool_calls = result.tool_calls + (7 if is_candidate else 0)
        latency = 50.0 if is_candidate else 1.0
        return result.model_copy(
            update={
                "output": output,
                "tool_calls": tool_calls,
                "measured_guardrails": {"latency": latency, "tool_calls": tool_calls},
            }
        )

class NoopCandidateAdapter(DeterministicFakeHermesAdapter):
    def run(self, request: AdapterRunRequest) -> AdapterRunResult:
        result = super().run(request)
        output = {"plan": ["generic triage"], "risk": [], "tests": [], "actionable_reference": ""}
        return result.model_copy(update={"output": output})


class BetterCandidateAdapter(DeterministicFakeHermesAdapter):
    def run(self, request: AdapterRunRequest) -> AdapterRunResult:
        result = super().run(request)
        if request.candidate_module_id:
            output = {
                "issue_reference": request.request_text,
                "plan": ["focused implementation in src/ultron/app/triage.py"],
                "risk": "pointer mutation regression",
                "tests": ["pytest tests/test_gap4_redteam.py -q"],
                "actionable_reference": "src/ultron/app/triage.py::approve_promotion",
            }
        else:
            output = {"plan": ["generic triage"], "risk": [], "tests": [], "actionable_reference": ""}
        return result.model_copy(update={"output": output})


class BenchmarkOnlyLiveStubAdapter(DeterministicFakeHermesAdapter):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def is_live(self) -> bool:
        return True

    @property
    def provider_id(self) -> str:
        return "live-provider"

    def run(self, request: AdapterRunRequest) -> AdapterRunResult:
        self.calls += 1
        result = super().run(request)
        if self.calls <= 1:
            return result.model_copy(update={"model_provider": "live-provider", "model_name": "real-model", "model_snapshot": {"provider": "live-provider", "name": "real-model"}})
        return result.model_copy(
            update={
                "model_provider": "live-provider",
                "model_name": "stub-model",
                "model_snapshot": {"provider": "live-provider", "name": "stub-model", "stub": True},
            }
        )


def _fixture(task_count: int = 3) -> BenchmarkFixture:
    return BenchmarkFixture(
        name="gap4-redteam",
        seed="fixed-seed",
        tasks=[
            BenchmarkTask(
                task_id=f"gap4-{index}",
                request_text=f"request {index}",
                rubric={"keywords": ["focused", "tests"]},
            )
            for index in range(task_count)
        ],
    )


def _quality_fixture(task_count: int = 10) -> BenchmarkFixture:
    return BenchmarkFixture(
        name="gap4-quality",
        seed="quality-seed",
        tasks=[
            BenchmarkTask(
                task_id=f"quality-{index}",
                request_text=f"Fix permission risk in approval path {index}",
                rubric={
                    "issue_keywords": ["permission", "risk", "approval"],
                    "requires_risk_section": True,
                    "requires_concrete_test": True,
                    "requires_actionable_reference": True,
                },
            )
            for index in range(task_count)
        ],
    )


def _promotable_tasks(count: int = 10) -> list[PairedTask]:
    return [PairedTask(task_id=f"task-{index}", baseline_metric=1.0, candidate_metric=1.2) for index in range(count)]


def test_no_fabricated_submit_metrics_and_promotion_requires_real_benchmark() -> None:
    server_source = open("src/ultron/app/server.py", encoding="utf-8").read()
    assert "PairedTask" not in server_source
    assert not re.search(r"PairedTask\s*\([^)]*(baseline_metric|candidate_metric)\s*=\s*1\.[02]", server_source, re.DOTALL)
    assert "candidate_metric=1.2" not in server_source
    assert "baseline_metric=1.0" not in server_source

    client = TestClient(create_app())
    csrf = client.get("/").cookies["ultron_csrf"]
    initial_pointer = client.app.state.triage.pointer_store.get(client.app.state.triage.pointer_key)

    submitted = client.post("/api/action", headers={"X-CSRF-Token": csrf}, json={"type": "SUBMIT_REQUEST", "payload": {"request_text": "triage this"}, "csrf_token": csrf})
    assert submitted.status_code == 200
    submitted_body = submitted.json()
    assert "evaluation" not in submitted_body
    candidate_hash = submitted_body["candidate"]["content_hash"]
    assert client.app.state.triage.has_promotable_evidence(candidate_hash) is False

    denied = client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={
            "type": "APPROVE_PROMOTION",
            "payload": {"candidate_hash": candidate_hash},
            "csrf_token": csrf,
            "active_pointer_version": client.app.state.triage.current_pointer_version(),
        },
    )
    assert denied.status_code == 403
    assert client.app.state.triage.pointer_store.get(client.app.state.triage.pointer_key) == initial_pointer

    benchmarked = client.post("/api/action", headers={"X-CSRF-Token": csrf}, json={"type": "RUN_BENCHMARK", "payload": {"candidate_hash": candidate_hash}, "csrf_token": csrf, "active_pointer_version": client.app.state.triage.current_pointer_version()})
    assert benchmarked.status_code == 200
    evaluation = benchmarked.json()["evaluation"]
    assert evaluation["report"]["evidence_label"] in {"BENCHMARK", EvidenceLabel.BENCHMARK.value}
    assert evaluation["report"]["paired_tasks"] >= 10
    assert evaluation["report"]["promotable"] is True
    assert client.app.state.triage.has_promotable_evidence(candidate_hash) is True

    before_approval = client.app.state.triage.pointer_store.get(client.app.state.triage.pointer_key)
    approved = client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={
            "type": "APPROVE_PROMOTION",
            "payload": {"candidate_hash": candidate_hash},
            "csrf_token": csrf,
            "active_pointer_version": before_approval[0],
        },
    )
    assert approved.status_code == 200
    assert approved.json()["decision"]["promoted"] is True
    assert client.app.state.triage.pointer_store.get(client.app.state.triage.pointer_key)[0] == before_approval[0] + 1
    assert candidate_hash in client.app.state.triage.pointer_store.get(client.app.state.triage.pointer_key)[1]


def test_manual_evaluate_promotable_floats_are_not_approval_evidence() -> None:
    app = TriageApp()
    app.seed_baseline()
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "manual-forgery"})
    candidate_hash = canary["candidate"].content_hash or ""
    before_pointer = app.pointer_store.get(app.pointer_key)

    decision = app.evaluate_and_decide(candidate_hash, _promotable_tasks(10), canary["canary_id"])

    assert decision["report"].promotable is True
    assert decision["report"].provenance == "manual"
    assert app.has_promotable_evidence(candidate_hash) is False
    with pytest.raises(PermissionError):
        app.approve_promotion(candidate_hash, before_pointer[0])
    assert app.pointer_store.get(app.pointer_key) == before_pointer

    with pytest.raises(TypeError):
        app.evaluate_and_decide(candidate_hash, _promotable_tasks(10), canary["canary_id"], provenance="benchmark_runner")


def test_no_manual_promotion_escape_hatch_attribute() -> None:
    app = TriageApp()

    assert not hasattr(app, "allow_manual_promotable_evidence")


def test_benchmark_evidence_requires_real_trajectory_ids() -> None:
    app = TriageApp()
    app.seed_baseline()
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "bound-evidence"})
    candidate_hash = canary["candidate"].content_hash or ""
    decision = app.benchmark_and_decide(candidate_hash, _quality_fixture(), canary["canary_id"])
    report = decision["report"]

    assert report.provenance == "benchmark_runner"
    assert report.benchmark_fixture_id == _quality_fixture().name
    assert len(report.benchmark_task_trajectory_ids) == report.paired_tasks
    assert all(len(ids) == 2 and all(item for item in ids) for ids in report.benchmark_task_trajectory_ids.values())
    assert app.has_promotable_evidence(candidate_hash) is True

    app.evaluated_candidates[candidate_hash]["report"] = report.model_copy(update={"benchmark_task_trajectory_ids": {}})
    assert app.has_promotable_evidence(candidate_hash) is False
    with pytest.raises(PermissionError):
        app.approve_promotion(candidate_hash, app.current_pointer_version())


def test_empty_risk_section_does_not_receive_risk_credit() -> None:
    rubric = {"requires_risk_section": True, "requires_concrete_test": True, "requires_actionable_reference": True}

    score = benchmark_module.score_output({"text": "risk:   \n tests: pytest tests/test_gap4_redteam.py assert\n source: src/ultron/app/triage.py"}, rubric)

    assert score == pytest.approx(2 / 3)


def test_quality_rubric_noop_candidate_does_not_beat_baseline_but_better_candidate_does() -> None:
    fixture = _quality_fixture()
    noop_runner = BenchmarkRunner(NoopCandidateAdapter(), lambda module_set, task, side: simple_adapter_request(module_set, task, side))
    noop_pairs = noop_runner.run_paired(["base"], ["base", "candidate"], fixture)
    assert all(pair.candidate_metric == pytest.approx(pair.baseline_metric) for pair in noop_pairs)
    assert sum(pair.candidate_metric - pair.baseline_metric for pair in noop_pairs) == pytest.approx(0.0)

    better_runner = BenchmarkRunner(BetterCandidateAdapter(), lambda module_set, task, side: simple_adapter_request(module_set, task, side))
    better_pairs = better_runner.run_paired(["base"], ["base", "candidate"], fixture)
    assert all(pair.candidate_metric > pair.baseline_metric for pair in better_pairs)


def test_live_stub_benchmark_is_rejected_without_stored_evidence() -> None:
    app = TriageApp(adapter=BenchmarkOnlyLiveStubAdapter())
    app.seed_baseline()
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "live-stub"})
    candidate_hash = canary["candidate"].content_hash or ""

    with pytest.raises(ValueError, match="stub/fake"):
        app.benchmark_and_decide(candidate_hash, _quality_fixture(), canary["canary_id"])

    assert candidate_hash not in app.evaluated_candidates
    assert app.has_promotable_evidence(candidate_hash) is False


def test_benchmark_runner_is_deterministic_adapter_backed_and_time_uuid_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture()
    first_adapter = SpyAdapter()
    runner = BenchmarkRunner(first_adapter, lambda module_set, task, side: simple_adapter_request(module_set, task, side))
    first = runner.run_paired(["baseline"], ["baseline", "candidate"], fixture)

    monkeypatch.setattr(benchmark_module.uuid, "uuid4", lambda: pytest.fail("uuid4 must not drive benchmark determinism"))
    monkeypatch.setattr(benchmark_module.uuid, "uuid5", lambda namespace, name: type("FakeUuid", (), {"hex": f"fixed-{abs(hash(name))}"})())
    monkeypatch.setattr(triage_module.time, "time", lambda: 1234567890.0)

    second_adapter = SpyAdapter()
    runner_again = BenchmarkRunner(second_adapter, lambda module_set, task, side: simple_adapter_request(module_set, task, side))
    second = runner_again.run_paired(["baseline"], ["baseline", "candidate"], fixture)

    assert first == second
    assert [(request.candidate_module_id is None, request.request_text) for request in first_adapter.requests] == [
        (True, task.request_text) if index % 2 == 0 else (False, task.request_text)
        for task in fixture.tasks
        for index in range(2)
    ]
    assert len(first_adapter.requests) == len(fixture.tasks) * 2
    assert len(second_adapter.requests) == len(fixture.tasks) * 2
    assert all(pair.candidate_metric > pair.baseline_metric for pair in first)


def test_guardrail_regression_blocks_even_when_score_improves_and_pointer_is_unchanged() -> None:
    app = TriageApp(adapter=RegressingGuardrailAdapter())
    app.thresholds.guardrail_tolerance = {"latency": 0.0, "tool_calls": 0.0}
    app.seed_baseline()
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "faster-score-slower-runtime"})
    candidate_hash = canary["candidate"].content_hash or ""
    before_pointer = app.pointer_store.get(app.pointer_key)

    manual_decision = app.evaluate_and_decide(
        candidate_hash,
        _promotable_tasks(),
        canary["canary_id"],
        GuardrailMetrics(latency=1.0, tool_calls=1),
        GuardrailMetrics(latency=2.0, tool_calls=2),
    )
    assert manual_decision["report"].mean_primary_delta > 0
    assert manual_decision["report"].promotable is False
    assert set(manual_decision["report"].guardrail_breaches) >= {"latency", "tool_calls"}
    assert app.pointer_store.get(app.pointer_key) == before_pointer

    benchmark_decision = app.benchmark_and_decide(candidate_hash, _fixture(task_count=10), canary["canary_id"])
    assert benchmark_decision["report"].mean_primary_delta > 0
    assert benchmark_decision["report"].promotable is False
    assert set(benchmark_decision["report"].guardrail_breaches) >= {"latency", "tool_calls"}
    assert app.has_promotable_evidence(candidate_hash) is False
    with pytest.raises(PermissionError):
        app.approve_promotion(candidate_hash, before_pointer[0])
    assert app.pointer_store.get(app.pointer_key) == before_pointer


def test_eval06_non_default_thresholds_are_consistent_and_forgery_is_rejected() -> None:
    thresholds = SelectionThresholds(min_paired_tasks=3, min_primary_improvement=0.05)
    selector = Selector(thresholds)
    outcome = selector.evaluate("candidate", 1.0, 1.06, 4, {}, {})

    assert outcome.promotable is True
    assert outcome.evidence_label is EvidenceLabel.BENCHMARK
    assert outcome.primary_delta == pytest.approx(0.06)
    assert outcome.paired_tasks == 4
    assert outcome.thresholds == thresholds

    with pytest.raises(ValidationError):
        SelectionOutcome(
            candidate_hash="forged-preference",
            evidence_label=EvidenceLabel.PREFERENCE,
            primary_delta=0.06,
            paired_tasks=4,
            guardrail_breaches=[],
            promotable=True,
            rationale="forged promotable preference",
            thresholds=thresholds,
        )

    with pytest.raises(ValidationError):
        SelectionOutcome(
            candidate_hash="forged-threshold",
            evidence_label=EvidenceLabel.BENCHMARK,
            primary_delta=0.06,
            paired_tasks=4,
            guardrail_breaches=[],
            promotable=True,
            rationale="forged default threshold bypass",
            thresholds=SelectionThresholds(),
        )

    default_outcome = Selector(SelectionThresholds()).evaluate("candidate", 1.0, 1.06, 4, {}, {})
    assert default_outcome.promotable is False
    assert default_outcome.evidence_label is EvidenceLabel.PREFERENCE
