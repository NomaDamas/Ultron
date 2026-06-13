## Summary
The provenance bypass is closed for the inspected GAP4 promotion path. Manual callers cannot mint approvable evidence through evaluate_and_decide because it has no provenance parameter and the harness emits manual provenance by default. Approvable evidence is stamped only by benchmark_and_decide after BenchmarkRunner.run_paired and live adapter validation.

## Analysis
- Spec compliance: src/ultron/app/triage.py:308-338 shows evaluate_and_decide accepts candidate_hash, paired_tasks, canary_id, and guardrails only; src/ultron/evaluation/harness.py:90-136 returns EvaluationReport with provenance manual. tests/test_gap4_redteam.py:197-208 verifies manual promotable metrics remain non approvable and passing provenance benchmark_runner raises TypeError.
- Benchmark provenance: src/ultron/app/triage.py:341-375 runs BenchmarkRunner.run_paired, validates each result with _validate_live_adapter_result, then stamps provenance benchmark_runner, benchmark_fixture_id, and benchmark_task_trajectory_ids. The trajectories are bound per fixture task from baseline and candidate AdapterRunResult. tests/test_gap4_redteam.py:217-234 covers valid evidence and rejection after removing trajectory IDs.
- Promotion gate: src/ultron/app/triage.py:392-415 requires stored report provenance benchmark_runner, non empty fixture id, trajectory id map cardinality matching paired_tasks, non blank string trajectory IDs, promotable evidence label, report, and outcome. approve_promotion delegates to that gate before pointer mutation. src/ultron/app/server.py:99-104 and 128-132 route API approval through the same gate.
- Escape hatch: repo search found no allow_manual_promotable_evidence in src; tests/test_gap4_redteam.py:211-214 asserts the attribute is absent.
- Risk scoring: src/ultron/evaluation/benchmark.py:120-132 calls _section_has_content for required risk; lines 157-167 reject blank, [], {}, -, none, and n/a. tests/test_gap4_redteam.py:237-242 proves a blank risk section receives no risk credit.
- Safety regression check: guardrail regression remains blocking in src/ultron/evolution/selection.py:14-33 and 67-117; tests/test_gap4_redteam.py:293-322 verifies score improvement does not promote when latency or tool_calls regress, and tests/test_gap4_redteam.py:254-267 verify live stub or fake benchmark evidence is rejected without storing evidence.

## Root Cause
Prior bypass allowed caller settable provenance to make fabricated paired metrics look like benchmark evidence. The fix moves benchmark provenance stamping into benchmark_and_decide after adapter executed paired runs and binds that stamp to fixture and task trajectory evidence.

## Findings
No blocking findings.

## Recommendations
1. Keep promotion approval centralized on has_promotable_evidence and avoid future alternate approval paths.
2. Consider tightening trajectory cardinality to require exactly two IDs per task in production code, matching the current test expectation; current non empty list validation still rejects empty or blank forgery but does not enforce baseline plus candidate cardinality.

## Architectural Status
CLEAR

## Code Review Recommendation
APPROVE

## Trade-offs
- Current implementation is simple and centralized, with a clear product policy boundary.
- Exact two trajectory validation would be stricter and closer to the paired run invariant, at the cost of slightly coupling EvaluationReport evidence validation to the current baseline and candidate benchmark shape.
