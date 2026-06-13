## Summary
GAP4 removes the old HTTP SUBMIT_REQUEST fabricated evaluation path and adds a deterministic adapter-backed benchmark path, but it is not safe to approve yet. The main blockers are that promotable evidence can still be created through the application service without benchmark provenance, and the default benchmark plus fake-adapter scoring gives any two-module candidate a deterministic win independent of actual candidate behavior.

## Analysis
Evidence reviewed: src/ultron/evaluation/benchmark.py, src/ultron/evolution/selection.py, src/ultron/app/triage.py, src/ultron/app/server.py, tests/test_gap4_eval.py, tests/test_g007_server.py, tests/test_g007_app.py, plus supporting harness, adapter, runtime, and durable tests.

EVAL-02 is partially satisfied at the HTTP route layer. server.py:81-86 returns run, candidate, and canary only for SUBMIT_REQUEST and no evaluation object. server.py:87-95 routes RUN_BENCHMARK to engine.benchmark_and_decide. _policy_ok at server.py:126-132 and approve_promotion at triage.py:389-396 require stored promotable evidence. However, TriageApp.evaluate_and_decide at triage.py:308-335 remains a public service method that accepts arbitrary caller-provided PairedTask metrics and stores them as promotion evidence, and durable tests still call it with synthetic PairedTask 1.0 to 1.2 before approval at tests/test_gap3_durable.py:51-64. That means promotion does not require evidence provenance from a benchmark execution at the application boundary.

EVAL-01 is partly satisfied in the production benchmark path. BenchmarkRunner.run_paired calls _execute for baseline and candidate per task at benchmark.py:78-95, and TriageApp.benchmark_and_decide constructs baseline and candidate module sets then runs the runner at triage.py:341-355. Determinism is mostly preserved by uuid5-derived IDs in benchmark.py:162-185 and triage.py:366-368 and no time or random in scoring. But BenchmarkRunner._execute accepts a resolver returning AdapterRunResult directly at benchmark.py:98-105, which is an adapter bypass seam, and the default fake-adapter benchmark is not meaningfully discriminative.

The most serious product issue is the default benchmark rubric and fake-adapter interaction. DEFAULT_CODE_TRIAGE_V0 includes module_hash_count_at_least: 2 on every task at benchmark.py:28-50, while DeterministicFakeHermesAdapter emits the same canned triage keywords for baseline and candidate and includes module_hashes from the request at adapter.py:96-110. Baseline usually has one module hash and candidate has baseline plus candidate, so every candidate receives the extra rubric point and deterministically clears the 10 percent threshold without demonstrating candidate-specific quality.

EVAL-04 is materially implemented. _guardrails_from_result maps adapter outputs and metrics into cost, latency, tool_calls, safety violations, render failures, and permission requests at benchmark.py:132-142, runner aggregates them per side, and Selector._guardrail_breaches blocks promotion when after exceeds before plus tolerance at selection.py:114-123. Tests cover latency and tool_calls blocking. However, benchmark results are not passed through _validate_live_adapter_result, which is used for start_run and propose but not benchmark at triage.py:497-523, so live benchmark evidence lacks the same anti-stub and fake guard as normal runs.

EVAL-06 is satisfied for SelectionOutcome construction. The model validator re-derives promotability with self.thresholds at selection.py:49-59, and Selector.evaluate records a threshold snapshot at selection.py:96-112. A secondary watch item remains that EvolutionLoop._outcome_is_promotable re-derives against the current selector thresholds rather than the recorded outcome thresholds at evolution/loop.py:206-214, which can make non-default threshold evidence unstable if thresholds change between evaluation and retention.

The updated g007 HTTP tests preserve the main evidence gate and no-pointer-change denial behavior at tests/test_g007_server.py:82-136, and the app loop now uses benchmark_and_decide for the successful path at tests/test_g007_app.py:29-35. The rollback and no-poisoning intent remains covered in tests/test_g007_app.py:39-51, but the negative path still relies on manually fabricated PairedTask values rather than a benchmark-generated negative case.

## Root Cause
The implementation removed one obvious fabricated server constant but did not introduce an execution-provenance boundary for evaluation reports, and the default deterministic benchmark confounds candidate exists as second module with candidate improved output. Evidence is represented as aggregate floats and labels rather than as benchmark-run-derived, adapter-validated artifacts tied to executed task IDs and results.

## Findings
- HIGH/BLOCKER - src/ultron/app/triage.py:308-335, src/ultron/app/triage.py:377-396, tests/test_gap3_durable.py:51-64: evaluate_and_decide can still mint promotable stored evidence from caller-supplied PairedTask metrics, and approve_promotion consumes it without checking benchmark provenance. Impact: application-level callers can promote without executing BenchmarkRunner. Fix: make promotable approval require a benchmark-generated report or provenance token, or split manual evaluation from promotable benchmark evidence and deny manual reports for auto-promotion.
- HIGH/BLOCKER - src/ultron/evaluation/benchmark.py:28-50, src/ultron/hermes/adapter.py:96-110, src/ultron/evaluation/benchmark.py:88-91: the default benchmark deterministically rewards candidates for having two module hashes, while the fake adapter emits identical canned quality keywords. Impact: any prompt-edit candidate can trivially win without a real behavioral difference. Fix: remove module-count-as-quality from the default rubric, score task-specific observable outputs attributable to candidate content, and add a test proving unchanged or no-op candidates do not promote.
- MEDIUM - src/ultron/evaluation/benchmark.py:98-105: BenchmarkRunner can accept an AdapterRunResult directly and bypass the adapter. Impact: the runner contract does not guarantee through-adapter execution for all uses. Fix: keep direct run functions only in a separate test helper or require adapter invocation in production runner.
- MEDIUM - src/ultron/app/triage.py:341-355, src/ultron/app/triage.py:497-523: benchmark adapter results are not validated by the live anti-stub guard. Impact: benchmark evidence can be accepted from a live adapter returning fake or stub metadata even though normal runs reject that. Fix: validate each baseline and candidate AdapterRunResult during benchmark execution or inject validation into BenchmarkRunner.
- LOW/WATCH - src/ultron/evolution/loop.py:206-214: retention re-derives against current selector thresholds, not the outcome recorded threshold snapshot. Impact: non-default threshold outcomes can become inconsistent if thresholds change between evaluation and approval. Fix: use outcome.thresholds for consistency checks, while separately enforcing any desired current-policy migration rule.

## Recommendations
1. Block approval until promotable evidence carries benchmark provenance and approval rejects manual or caller-float reports.
2. Replace the default rubric module-count advantage with candidate-output quality checks that unchanged and no-op candidates fail.
3. Remove or isolate the AdapterRunResult bypass in BenchmarkRunner.
4. Run live-result validation on benchmark executions before storing or approving evidence.
5. Add tests: no-op candidate does not promote, measured-only tool_calls regression blocks, manual evaluate_and_decide report cannot approve, non-default threshold evidence remains consistent across validation and retention policy.

## Architectural Status
BLOCK

## Code Review Recommendation
REQUEST CHANGES

## Trade-offs
| Option | Benefit | Cost/Risk |
|---|---|---|
| Provenance-gate approval on BenchmarkRunner outputs | Strongly closes EVAL-02 and prevents caller-float evidence | Requires report schema and store change |
| Keep evaluate_and_decide as a generic helper but mark reports non-promotable unless benchmark-signed | Preserves test and manual evaluation utility | More state and provenance fields |
| Remove module-count rubric and score task-specific adapter output | Prevents trivial candidate wins | Need richer deterministic fixture outputs |
| Keep direct AdapterRunResult resolver for tests only | Test convenience | Must ensure production path cannot bypass adapter |
