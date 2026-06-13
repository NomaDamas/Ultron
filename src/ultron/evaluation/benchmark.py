"""Deterministic adapter-backed benchmark evaluation."""

from __future__ import annotations

import uuid
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ultron.evaluation.harness import GuardrailMetrics, PairedTask
from ultron.hermes.adapter import AdapterRunRequest, AdapterRunResult, HermesAdapter
from ultron.module.model import PersistencePolicy


class BenchmarkTask(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    task_id: str
    request_text: str
    rubric: dict[str, Any] = Field(default_factory=dict)
    weight: float = 1.0


def _issue_keywords(request: str) -> list[str]:
    stopwords = {"a", "an", "the", "for", "with", "while", "after", "only", "and", "or", "in", "on", "to", "of"}
    words = [word.strip(".,:;!?()[]{}'").lower() for word in request.split()]
    return [word for word in words if len(word) >= 4 and word not in stopwords][:3]


class BenchmarkFixture(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    name: str
    seed: str
    tasks: list[BenchmarkTask]

    @classmethod
    def default_code_triage_v0(cls) -> "BenchmarkFixture":
        criteria = {
            "issue_keywords": [],
            "requires_risk_section": True,
            "requires_concrete_test": True,
            "requires_actionable_reference": True,
        }
        requests = [
            "Fix a flaky pytest that fails only on CI",
            "Add validation for an API payload without broad rewrites",
            "Investigate a latency regression in request routing",
            "Patch a permission check while preserving rollback behavior",
            "Triage a failing migration with stale pointer risk",
            "Plan a UI action change with CSRF safety",
            "Debug a tool allowlist mismatch in a canary run",
            "Assess a candidate prompt pack for regression risk",
            "Repair evidence-gated promotion after benchmark failure",
            "Choose focused tests for a module registry change",
        ]
        return cls(
            name="code_triage_v0",
            seed="code-triage-v0-deterministic",
            tasks=[
                BenchmarkTask(
                    task_id=f"code-triage-v0-{index:02d}",
                    request_text=request,
                    rubric={**criteria, "issue_keywords": _issue_keywords(request)},
                    weight=1.0,
                )
                for index, request in enumerate(requests, start=1)
            ],
        )


DEFAULT_CODE_TRIAGE_V0 = BenchmarkFixture.default_code_triage_v0()


class ModuleSetResolver(Protocol):
    def __call__(self, module_set: Any, task: BenchmarkTask, side: str) -> AdapterRunRequest: ...


RunFn = Callable[[AdapterRunRequest], AdapterRunResult]


class BenchmarkRunner:
    def __init__(self, adapter: HermesAdapter, resolver_or_run_fn: ModuleSetResolver | RunFn) -> None:
        self.adapter = adapter
        self.resolver_or_run_fn = resolver_or_run_fn
        self.guardrails_before = GuardrailMetrics()
        self.guardrails_after = GuardrailMetrics()
        self.results: list[AdapterRunResult] = []

    def run_paired(self, baseline_module_set: Any, candidate_module_set: Any, fixture: BenchmarkFixture) -> list[PairedTask]:
        pairs: list[PairedTask] = []
        self.results = []
        before = GuardrailMetrics()
        after = GuardrailMetrics()
        for task in fixture.tasks:
            baseline_result = self._execute(baseline_module_set, task, "baseline")
            candidate_result = self._execute(candidate_module_set, task, "candidate")
            self.results.extend([baseline_result, candidate_result])
            before = _add_guardrails(before, _guardrails_from_result(baseline_result))
            after = _add_guardrails(after, _guardrails_from_result(candidate_result))
            pairs.append(
                PairedTask(
                    task_id=task.task_id,
                    baseline_metric=score_output(baseline_result.output, task.rubric),
                    candidate_metric=score_output(candidate_result.output, task.rubric),
                )
            )
        self.guardrails_before = before
        self.guardrails_after = after
        return pairs

    def _execute(self, module_set: Any, task: BenchmarkTask, side: str) -> AdapterRunResult:
        request = self.resolver_or_run_fn(module_set, task, side)  # type: ignore[misc]
        if not isinstance(request, AdapterRunRequest):
            raise TypeError("benchmark resolver must return AdapterRunRequest")
        return self.adapter.run(request)


def score_output(output: Any, rubric: dict[str, Any]) -> float:
    checks: list[bool] = []
    issue_keywords = rubric.get("issue_keywords")
    keywords = [str(item).lower() for item in (issue_keywords if issue_keywords is not None else rubric.get("keywords", []))]
    text = _flatten_output(output).lower()
    issue_text = _issue_reference_text(output).lower() if issue_keywords is not None else text
    checks.extend(keyword in issue_text for keyword in keywords)
    if rubric.get("requires_risk_section"):
        checks.append(_section_has_content(text, "risk"))
    if rubric.get("requires_concrete_test"):
        checks.append(_has_concrete_test(text))
    if rubric.get("requires_actionable_reference"):
        checks.append(_has_actionable_reference(text))
    if not checks:
        return 0.0
    return sum(1 for passed in checks if passed) / len(checks)


def benchmark_guardrails(before: GuardrailMetrics, after: GuardrailMetrics) -> tuple[GuardrailMetrics, GuardrailMetrics]:
    return before, after


def _flatten_output(output: Any) -> str:
    if isinstance(output, dict):
        return "\n".join(f"{key} {_flatten_output(value)}" for key, value in sorted(output.items()))
    if isinstance(output, list):
        return "\n".join(_flatten_output(item) for item in output)
    return str(output)

def _issue_reference_text(output: Any) -> str:
    if isinstance(output, dict):
        values = [output.get("issue"), output.get("issue_reference"), output.get("task_issue")]
        return "\n".join(str(value) for value in values if value)
    return _flatten_output(output)



def _section_has_content(text: str, section: str) -> bool:
    marker = f"{section}:"
    if marker not in text:
        marker = f"{section} "
    index = text.find(marker)
    if index < 0:
        return False
    rest = text[index + len(marker):].strip()
    first_line = rest.splitlines()[0].strip() if rest else ""
    return bool(first_line and first_line not in {"[]", "{}"})


def _has_concrete_test(text: str) -> bool:
    return "test" in text and any(token in text for token in ("pytest", "tests/", "assert", "unit", "e2e"))


def _has_actionable_reference(text: str) -> bool:
    return any(token in text for token in ("src/", "tests/", ".py", "::", "function", "method"))


def _guardrails_from_result(result: AdapterRunResult) -> GuardrailMetrics:
    measured = result.measured_guardrails
    return GuardrailMetrics(
        cost=float(measured.get("cost", 0) or 0),
        latency=float(measured.get("latency", measured.get("latency_ms", 0)) or 0),
        tool_calls=int(result.tool_calls or measured.get("tool_calls", 0) or 0),
        safety_violations=int(bool(measured.get("workspace_writes", False))) + len(measured.get("unknown_tools", []) or []),
        render_failures=int(measured.get("render_failures", 0) or 0),
        permission_requests=int(measured.get("permission_requests", 0) or 0),
    )


def _add_guardrails(left: GuardrailMetrics, right: GuardrailMetrics) -> GuardrailMetrics:
    return GuardrailMetrics(
        cost=left.cost + right.cost,
        latency=left.latency + right.latency,
        tool_calls=left.tool_calls + right.tool_calls,
        safety_violations=left.safety_violations + right.safety_violations,
        rollback_rate=left.rollback_rate + right.rollback_rate,
        corrections=left.corrections + right.corrections,
        render_failures=left.render_failures + right.render_failures,
        permission_requests=left.permission_requests + right.permission_requests,
        privacy_violations=left.privacy_violations + right.privacy_violations,
        variant_count=left.variant_count + right.variant_count,
        composition_conflicts=left.composition_conflicts + right.composition_conflicts,
        mistaken_pruning_restores=left.mistaken_pruning_restores + right.mistaken_pruning_restores,
    )


def simple_adapter_request(module_hashes: list[str], task: BenchmarkTask, side: str, *, active_module_set_hash: str | None = None) -> AdapterRunRequest:
    run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{side}:{task.task_id}:{','.join(module_hashes)}").hex
    session_id = uuid.uuid5(uuid.NAMESPACE_URL, f"session:{side}:{task.task_id}:{','.join(module_hashes)}").hex
    module_set_hash = active_module_set_hash or "benchmark-" + "-".join(hash_value[:12] for hash_value in module_hashes)
    return AdapterRunRequest(
        run_id=run_id,
        session_id=session_id,
        user_scope="default-user",
        workflow_fingerprint="code-triage",
        active_module_set_id=f"benchmark:{side}",
        active_module_set_hash=module_set_hash,
        ordered_module_hashes=list(module_hashes),
        candidate_module_id=module_hashes[-1] if side == "candidate" and len(module_hashes) > 1 else None,
        canary_id=None,
        persistence_mode=PersistencePolicy.ISOLATED,
        isolated_root=f"/tmp/ultron/benchmark/{session_id}",
        resolved_prompt_order=[],
        resolved_tool_allowlist=["read", "search", "pytest"],
        resolved_skill_refs=[],
        budget_policy={"max_tool_calls": 3},
        safety_policy={"workspace_writes": False, "external_calls": False},
        ui_spec_hash=None,
        request_text=task.request_text,
    )
