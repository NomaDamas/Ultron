## Summary
GAP6 has the right fail-closed direction for deterministic fakes and live stubs, and the current test suite shows 269 passed and 3 skipped via artifacts/full-suite.txt. The quality gate is blocked because both generative seams rely on the current generator or synthesizer implementation to validate its own output; TriageApp does not re-validate returned UiSpecs or synthesized modules at the server-owned seam boundary, so a future live implementation can bypass the critical invariants.

## Analysis
Evidence inspected: src/ultron/ui/generator.py, src/ultron/synthesis/module_synthesizer.py, src/ultron/evolution/planner.py, src/ultron/app/triage.py, src/ultron/app/server.py, src/ultron/ui/runtime.py, tests/test_gap6_generative.py, tests/test_gap6_redteam.py, artifacts/gap6-qa.txt, artifacts/full-suite.txt.

UI security: validate_generated_uispec parses UiSpec, calls UiSpec.finalized(registry), and rejects privileged ActionType values in component.props.actions. UiSpec.validate rejects component types not in the provided registry. ActionCommand and validate_action gate privileged server actions with session, CSRF, pointer version, and policy checks. However TriageApp._generate_uispec returns self.ui_generator.generate(...) directly, and /api/uispec returns engine.current_uispec() directly. The deterministic fake validates internally, and the live stub raises LiveModelUnavailable, but the seam boundary is not fail-closed for a future live generator or injected generator that returns an already parsed UiSpec without calling validate_generated_uispec.

Synthesis safety: DeterministicFakeModuleSynthesizer intersects parent and allowed surfaces, stores real blob-backed policy artifacts, sets PromotionState.CANDIDATE, and calls validate_synthesized_module. TriageApp.synthesize_candidate registers the returned module as ModuleLifecycle.CANDIDATE on the canary layer, creates canary scoped state, and reports promotable false unless benchmark provenance exists. approve_promotion remains benchmark-provenance gated by has_promotable_evidence. However synthesize_candidate does not re-run validate_synthesized_module at the app boundary, and the fake calls validate_synthesized_module with registry=None, so registry.can_auto_promote is not exercised on the synthesis path. A future live synthesizer can return a permission-expanding or non-auto-promotable draft and have it registered as a canary candidate unless its own implementation self-validates.

VariationPlanner: plan returns a PendingVariationApproval when variant budget is exhausted, when indicated tools are outside parent tools, when topology is touched, or when compound changes have more than one field. Otherwise it emits a single MutationProposal with exactly one primitive/change and no human approval. It is deterministic and does not apply changes itself.

Live stubs: LiveModelUiSpecGenerator.generate and LiveModelModuleSynthesizer.synthesize build prompts and raise LiveModelUnavailable with no live model provider imports. test_gap6_redteam additionally runs a subprocess check for banned model modules.

No regression evidence: artifacts/full-suite.txt reports 269 passed, 3 skipped, 1 warning. Prior gates for benchmark-provenance promotion and canary isolation remain present in TriageApp.approve_promotion, has_promotable_evidence, register CANDIDATE paths, and rollback/canary stores.

## Root Cause
The root defect is misplaced trust boundary enforcement. Validation helpers exist, but the server-owned consumers of generated artifacts do not wrap every generator and synthesizer result with those helpers. The contract is therefore convention-based per implementation rather than enforced at the seam.

## Findings
- HIGH, src/ultron/app/triage.py:628-637 and src/ultron/app/server.py:39-45: _generate_uispec trusts self.ui_generator.generate and /api/uispec returns it without validate_generated_uispec. Impact: a future live or injected UiSpecGenerator can bypass the registry and privileged/model-defined action rejection. Fix: import validate_generated_uispec in triage.py and always return validate_generated_uispec(self.ui_generator.generate(context), self.ui_registry) at _generate_uispec, with tests using a malicious generator injected into app.ui_generator.
- HIGH, src/ultron/app/triage.py:455-486 and src/ultron/synthesis/module_synthesizer.py:91-132: synthesize_candidate trusts self.module_synthesizer.synthesize and the fake validates with registry=None, so can_auto_promote is not enforced at the app seam. Impact: a future live synthesizer can register a permission-expanding or otherwise non-auto-promotable candidate into canary. Fix: TriageApp must call validate_synthesized_module(candidate, self.adapter_contract, parent=parent, registry=self.registry) before registry.register, and tests must inject a malicious synthesizer.
- MEDIUM, src/ultron/synthesis/module_synthesizer.py:118-132: validate_synthesized_module finalizes before checking content_hash, making the mismatch check tautological. Impact: supplied model hash mismatches are normalized silently rather than rejected, weakening post-model validation evidence. Fix: compare any supplied module.content_hash to module.compute_content_hash before finalization, then return the finalized server-owned identity.
- LOW, src/ultron/synthesis/module_synthesizer.py:135-174: local _expands_permissions does not mirror registry._expands_permissions and omits required_adapter_capabilities and CHECKPOINTED rank. Impact: duplicated policy can drift from the registry gate. Fix: use registry.can_auto_promote at the seam, or centralize the permission expansion predicate.

## Recommendations
1. Move validation enforcement to TriageApp seam boundaries for UiSpec and synthesized modules, not only inside fake generators.
2. Add red-team tests that install malicious fake UiSpecGenerator and ModuleSynthesizer implementations on TriageApp and verify /api/uispec and synthesize_candidate fail closed.
3. Make content-hash mismatch validation explicit before finalization.
4. Keep live stubs fail-closed and provider-import-free.

## Architectural Status
BLOCK

## Code Review Recommendation
REQUEST CHANGES

## Trade-offs
- Boundary validation in TriageApp: best option. Small duplication, but turns future generator implementations into untrusted producers and keeps server policy centralized.
- Validation inside each generator/synthesizer: current approach. Less wrapper code, but unsafe because future implementations can bypass the helper.
- Registry-only promotion gate: necessary but insufficient. It prevents promotion, not unsafe canary registration or execution surface expansion.
