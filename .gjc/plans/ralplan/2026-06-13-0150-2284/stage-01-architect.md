## Summary
GAP5 is architecturally safe for promotion and identity: feedback is reduced to non-promotable preference evidence, approval remains gated by benchmark_runner provenance plus complete trajectory ids, and fitness updates mutate only the stored module fitness JSON while HarnessModule.content_hash remains design-derived. Product risk is WATCH rather than CLEAR because automatic atrophy can be executed through the durable path without re-checking G005 critical-seed approval/cooldown/floor in the same transaction, even though the in-memory GAP5 scan currently relies on EvolutionLoop.atrophy_scan to pre-filter.

## Analysis
Evidence-backed findings:
- Promotion safety: src/ultron/app/triage.py:405-419 requires stored evaluation evidence, benchmark_runner provenance, a benchmark_fixture_id, trajectory ids for every paired task, a BENCHMARK/CAUSAL_SUFFICIENT evidence label, and both report.promotable and outcome.promotable. Feedback summary is only appended into mutable fitness metadata at src/ultron/app/triage.py:247-248 and 506-512; it does not write evaluated_candidates or alter approval evidence. src/ultron/feedback/aggregation.py:50-51 caps aggregation at EvidenceLabel.PREFERENCE.
- Fitness identity stability: src/ultron/module/model.py:98-115 excludes fitness and content_hash from identity_fields/compute_content_hash, and src/ultron/app/triage.py:514-528 updates only module.fitness then stores the updated module under the existing registry key. tests/test_gap5_feedback.py:87-108 covers run and benchmark updates preserving content_hash.
- Canonical hashing/privacy: src/ultron/app/triage.py:218-243 uses sha256(json.dumps(canonical_rating_payload(...), sort_keys=True, separators=(",", ":"))) with PRODUCT_IMPROVEMENT, redacted, and 30d retention. src/ultron/feedback/aggregation.py:56-57 strips comments before hashing, so whitespace-only comment differences collapse deterministically; this is acceptable redaction/canonicalization but should be intentional product behavior.
- Aggregation safety: src/ultron/feedback/aggregation.py:13 and 30 count only EXPLICIT_USER/VERIFIED_SYSTEM; MODEL_GENERATED is excluded. src/ultron/feedback/channel.py:69-74 and 86-93 prevent model-generated outcome verification; purge_expired at src/ultron/feedback/channel.py:119-128 preserves retention semantics.
- Auto-atrophy: src/ultron/app/triage.py:470-489 delegates eligibility to EvolutionLoop.atrophy_scan, then uses EvolutionLoop.prune for in-memory ledgered pointer transition or PromotionUnitOfWork.prune for durable pointer/lifecycle/ledger atomicity. src/ultron/evolution/loop.py:126-144 implements floor, critical-seed skip, cooldown, and stale/negative/rollback/conflict criteria. tests/test_gap5_feedback.py:110-135 verifies in-memory prune/restore/floor/critical-seed behavior.

Architecture:
- Approval remains a narrow, explicit boundary: only evaluated_candidates produced by benchmark_and_decide can satisfy has_promotable_evidence. Manual evaluate_and_decide remains provenance="manual" and is rejected by the GAP4 gate.
- Runtime fitness is treated as mutable registry metadata. The current implementation stores it inside module_json, but identity is stable because compute_content_hash excludes fitness; this is a pragmatic MVP tradeoff, not a pure immutable-record design.
- The atrophy architecture reuses G005 scan/prune/restore rather than forking eligibility scoring, but the durable run_atrophy_scan branch takes precomputed new_active and calls UoW.prune directly. The UoW provides atomic ledgering but does not itself validate critical seeds, cooldown, or diversity floor; correctness depends on the caller prior scan and length check.

Code/security/performance:
- No Python hash() remains in the reviewed feedback hashing path; tests/test_gap5_redteam.py:105-123 monkeypatches builtins.hash/time/uuid and proves deterministic payload_hash.
- Fitness decay is deterministic math only at src/ultron/app/triage.py:636-643; time is used only as last_used_at/timestamp, not identity.
- Tests are good for in-memory GAP5 behavior and red-team cases, but durable run_atrophy_scan is not directly tested; durable tests cover atrophy_and_restore UoW ledgering, not scheduled scan pruning.

## Root Cause
The only meaningful architectural concern is that scheduled atrophy durable branch bypasses EvolutionLoop.prune and calls PromotionUnitOfWork.prune directly after a separate atrophy_scan. This separates policy validation from atomic durable mutation, so future callers or state drift between scan and prune could bypass critical-seed/cooldown/floor guarantees unless UoW or a shared transition planner enforces them.

## Findings
- MEDIUM — src/ultron/app/triage.py:470-489 and src/ultron/persistence/unit_of_work.py:24-57: Durable scheduled atrophy relies on pre-filtered eligible results and a caller-side floor check, while PromotionUnitOfWork.prune does not revalidate critical seeds, prune cooldown, or diversity floor. Impact: policy safety depends on caller discipline rather than the durable mutation boundary. Fix: add a shared validated prune plan or make UoW.prune accept/enforce StabilityControls and critical-seed/cooldown state before pointer/lifecycle mutation.
- LOW — tests/test_gap5_feedback.py:110-135: GAP5 scheduled atrophy tests cover only the in-memory path. Impact: durable scheduled prune ledgering and rollback invariants are inferred from generic UoW tests, not the new run_atrophy_scan integration. Fix: add a durable build_durable_triage_app_for_tests run_atrophy_scan case asserting pointer transition ledger, lifecycle, and restart visibility.
- LOW — src/ultron/app/triage.py:218 and src/ultron/feedback/aggregation.py:56-57: canonical_rating_payload strips comment whitespace, so " useful " and "useful" hash identically. Impact: deterministic and privacy-sane, but the exact product semantics are normalized comments rather than byte-preserving comments. Fix: keep as-is if intended; document the normalization contract if external clients rely on hashes.

## Recommendations
1. Keep promotion approval gated exactly as implemented; do not feed feedback_summary into evaluated_candidates or has_promotable_evidence.
2. Move scheduled atrophy prune validation into a shared policy boundary used by both in-memory and durable paths, or extend PromotionUnitOfWork.prune with explicit controls/critical/cooldown validation.
3. Add durable run_atrophy_scan tests covering ledger rows, lifecycle, floor, cooldown, and critical seed after restart.
4. Document rating payload canonicalization as redacted/normalized comment plus int rating.

## Architectural Status
WATCH

## Code Review Recommendation
COMMENT

## Trade-offs
- Current durable UoW-only scheduled prune: atomic and ledgered, minimal code, but policy assumptions live in caller-side pre-scan.
- Shared validated prune planner/UoW validation: more plumbing and state passing, but makes policy invariants local to the durable mutation boundary.
- Separate durable scheduled scan test only: cheaper and useful, but does not eliminate the architectural bypass risk by itself.
