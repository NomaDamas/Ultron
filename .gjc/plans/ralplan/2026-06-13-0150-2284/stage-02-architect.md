## Summary
The two prior GAP1 blockers are resolved. Tool policy compilation is centralized in TriageApp._build_adapter_request, and PinnedHermesAdapter consumes the Hermes-native allowlist without recompilation; non-default pointer bootstrap is now committed only after live adapter validation succeeds.

## Analysis
Spec compliance: src/ultron/hermes/adapter.py documents AdapterRunRequest.resolved_tool_allowlist as Hermes-native names compiled once by TriageApp._build_adapter_request. src/ultron/hermes/adapter.py build_invocation_plan assigns hermes_tool_allowlist = list(request.resolved_tool_allowlist) and performs no ToolPolicyCompiler call. src/ultron/app/triage.py _build_adapter_request calls ToolPolicyCompiler.compile(manifest.resolved_tool_allowlist) and stores compiled_tools.hermes_tools into AdapterRunRequest.resolved_tool_allowlist.

Test coverage: tests/test_gap1_adapter.py test_triage_builder_to_pinned_adapter_preserves_compiled_native_tools constructs a request through TriageApp._build_adapter_request, computes expected native tools from logical read/search/pytest, asserts expected_tools is non-empty, then asserts both the request and PinnedHermesAdapter plan carry the same native list. The compiler mapping test in the same file verifies that read/search/pytest become read_file/search_files/terminal_process.

Pointer bootstrap: src/ultron/app/triage.py start_run stages the default active pointer in local active and should_bootstrap_pointer for an empty non-default scope, then calls adapter.run and _validate_live_adapter_result before pointer_store.swap. tests/test_gap1_redteam.py test_live_guard_rejects_non_default_scope_without_pointer_bootstrap covers a rejecting live adapter and asserts the non-default pointer remains (0, []), last_manifest is None, and no ledger entries are written for the attempted run.

Regression assessment: the changed boundaries are explicit and local. The adapter seam now receives a native tool contract and no longer owns translation. The pointer bootstrap path avoids poisoning active scope state on live validation failure. No inspected regression blocks approval.

## Root Cause
The original blockers were caused by duplicated ownership of tool translation across TriageApp and PinnedHermesAdapter, plus an eager non-default pointer bootstrap that mutated scope state before the live adapter result had passed validation.

## Findings
None.

## Recommendations
1. Keep ToolPolicyCompiler ownership in TriageApp._build_adapter_request and treat AdapterRunRequest.resolved_tool_allowlist as Hermes-native API surface.
2. Keep pointer_store.swap for bootstrapped non-default scopes after adapter.run and _validate_live_adapter_result.
3. Preserve the current regression tests because they exercise the exact former failure modes.

## Architectural Status
CLEAR

## Code Review Recommendation
APPROVE

## Trade-offs
- Centralized compilation in TriageApp gives a single policy boundary and simpler adapter contracts at the cost of requiring request builders to honor the native-list invariant.
- Deferred pointer bootstrap prevents state poisoning on rejected live results at the cost of doing the live adapter call before the non-default pointer is materialized.
