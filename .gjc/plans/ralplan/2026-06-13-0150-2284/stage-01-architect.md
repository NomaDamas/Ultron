## Summary
GAP1 is not ready to approve. The adapter seam is present and canary rejection is staged, but live start-run validation can still leave a pointer mutation on rejection and the real pinned plan drops tools via double compilation.

## Analysis
Evidence inspected: src/ultron/hermes/adapter.py, src/ultron/hermes/tool_policy.py, src/ultron/composition/manifest.py, src/ultron/composition/resolver.py, src/ultron/run/manifest.py, src/ultron/app/triage.py, tests/test_gap1_adapter.py, tests/test_gap1_redteam.py, tests/test_gap1_vendor_contract.py, and related G007 rollback/promotion tests.

Seam integrity is mostly satisfied: production triage calls adapter.run in start_run and propose_and_canary, and source search found no _simulate_adapter_run under src. Fake determinism is satisfied: DeterministicFakeHermesAdapter derives outputs from AdapterRunRequest content and has no time/uuid/random path; tests monkeypatch time/uuid. skill_refs are carried resolver to ModuleSetManifest hash to RunManifest to AdapterRunRequest, with layer-order dedupe and deferred fail-closed tests. G007 rollback/promotion evidence still appears intact in tests.

## Root Cause
The implementation splits responsibility for translating logical tool names to Hermes-native tools across both TriageApp._build_adapter_request and PinnedHermesAdapter.build_invocation_plan, so the live adapter boundary is ambiguous. It also treats non-default pointer bootstrapping as pre-run setup, but under the live guard acceptance criterion it is a real mutation that must be staged until validation succeeds.

## Findings
- HIGH: src/ultron/app/triage.py:142-147 mutates pointer_store for a previously unseen non-default scope/workflow before adapter.run and before _validate_live_adapter_result. A rejected live result leaves that pointer behind. Fix: stage the fallback active set locally and commit the pointer bootstrap only after live validation succeeds, or avoid the mutation entirely before validation; add a non-default-scope rejection test asserting pointer_store remains unchanged.
- HIGH: src/ultron/app/triage.py:366-382 compiles manifest logical tools into native names, then src/ultron/hermes/adapter.py:158-175 compiles request.resolved_tool_allowlist again. Since ToolPolicyCompiler maps logical names, live requests built by TriageApp produce an empty pinned Hermes tool allowlist. Fix: compile exactly once at the adapter boundary and add an end-to-end TriageApp to PinnedHermesAdapter plan test.

## Recommendations
1. Fix pointer bootstrapping so all live rejection paths are mutation-free before validation, including non-default start_run.
2. Define AdapterRunRequest.resolved_tool_allowlist as either logical or native and compile only once; align tests to the production request path.
3. Keep current canary staging, provider/name/marker/provider_id guard, skill_refs, and rollback evidence tests.

## Architectural Status
BLOCK

## Code Review Recommendation
REQUEST CHANGES

## Trade-offs
- Keep request allowlist logical: adapter owns translation; clearer real adapter seam, but fake sees logical names.
- Keep request allowlist native: triage owns translation; PinnedHermesAdapter must not recompile and ToolPolicyCompiler unknowns must be surfaced before request construction.
