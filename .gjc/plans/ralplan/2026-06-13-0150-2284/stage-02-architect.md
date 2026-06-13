## Summary
The three GAP6 boundary-enforcement fixes are resolved in the inspected paths. Generator and synthesizer outputs are revalidated at the TriageApp boundary before serve/store or candidate registration, and synthesized content-hash integrity is now checked against recomputed identity bytes before finalize.

## Analysis
Spec compliance: `TriageApp._generate_uispec` is the only `ui_generator.generate(...)` call in `src/ultron/app/triage.py` and returns `validate_generated_uispec(generated, self.ui_registry)` before callers assign `last_ui_spec` or include a spec in a run/canary response. The serve paths `current_uispec`, `start_run`, and `propose_and_canary` all route through `_generate_uispec`; `/api/uispec` calls `engine.current_uispec()` in `src/ultron/app/server.py`.

Synthesis boundary: `TriageApp.synthesize_candidate` wraps `self.module_synthesizer.synthesize(context)` in `validate_synthesized_module(..., parent=parent, registry=self.registry)` before deriving `candidate_hash`, calling `registry.register`, registering the candidate, writing canary state, tracking rollback, or setting `last_candidate_hash` / `last_canary_id`.

Content identity: `validate_synthesized_module` captures `declared_hash = module.content_hash`, computes `recomputed_hash = module.compute_content_hash()`, rejects mismatches, and only then calls `module.finalized()`. This makes the check non-tautological because it compares incoming declared hash to freshly recomputed identity bytes before overwriting content_hash.

Tests: `tests/test_gap6_redteam.py` covers injected malicious UI generator rejection with `last_ui_spec` remaining unset, injected permission-expanding synthesizer rejection with unchanged pointer/registry/last candidate/canary state, and tampered declared content_hash rejection. The provided full-suite evidence reports 277 passed / 3 skipped, with the local artifact `artifacts/full-suite.txt` showing the earlier full run passed.

## Root Cause
The prior blockers were boundary trust violations: deterministic seams validated internally, but application boundaries could accept substituted generator/synthesizer outputs without revalidating them before serving, storing, or registering. The fix centralizes validation at the application boundary and performs identity verification before normalization.

## Findings
None requiring changes.

LOW, `src/ultron/synthesis/module_synthesizer.py`: the local `_expands_permissions` helper is slightly narrower than `ModuleRegistry.can_auto_promote` because registry expansion also considers declared surface names and required capabilities. Impact is limited because `validate_synthesized_module(..., registry=self.registry)` immediately applies `registry.can_auto_promote(candidate)` in the app boundary; standalone validation with `registry=None` remains less comprehensive. Fix only if this validator is intended to be reused outside app registration boundaries: consolidate on the registry/shared expansion helper.

## Recommendations
1. Approve GAP6 boundary fixes.
2. Keep the app-level revalidation as the authoritative boundary even when deterministic fake generators/synthesizers validate internally.
3. Consider consolidating duplicate `_expands_permissions` logic to prevent future drift between synthesis validation and registry promotion policy.

## Architectural Status
CLEAR

## Code Review Recommendation
APPROVE

## Trade-offs
- Boundary revalidation costs a small amount of duplicate validation but prevents injected/live seams from bypassing server-owned policy.
- Keeping deterministic seam validation is useful defense-in-depth, but it must not replace the app boundary check.
- Consolidating permission-expansion logic reduces drift but introduces coupling from synthesis validation to registry policy; current app path already gets the stricter registry check.
