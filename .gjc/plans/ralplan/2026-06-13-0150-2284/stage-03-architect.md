## Summary
Durable promotion cap/eviction is resolved. The durable approve path now computes the same active-set transition plan as in-memory retain, performs candidate promotion, evictions, pointer CAS, and ledger append inside a single SQLite unit-of-work transaction, with regression coverage for parity and rollback.

## Analysis
Evidence inspected:
- src/ultron/evolution/loop.py:24-60 defines ActiveSetTransitionPlan and plan_active_set_transition; EvolutionLoop.retain uses it at lines 112-123, preserving the existing pointer swap then lifecycle updates for in-memory flow while centralizing cap/eviction selection.
- src/ultron/persistence/unit_of_work.py:22-63 passes active_module_cap into promote, recomputes plan_active_set_transition inside db.tx() from the current durable pointer, updates the candidate to SURVIVOR, marks plan evictions PRUNED, performs pointer _swap_in_tx CAS to the capped set, and appends a single POINTER_TRANSITION ledger entry with evicted_hashes before commit.
- src/ultron/app/triage.py:348-360 routes durable approve_promotion through PromotionUnitOfWork.promote with active_module_cap and no uncapped pre-append; the non-SQLite path remains EvolutionLoop.retain.
- tests/test_gap3_durable.py:160-230 verifies durable active-set parity with plan_active_set_transition, cap never exceeded, evicted lifecycle PRUNED, eviction ledgered, restore reversibility, and rollback of pointer/lifecycles/ledger on injected mid-transaction ledger failure.
- src/ultron/persistence/db.py:29-53 confirms db.tx() uses BEGIN IMMEDIATE and rolls back on exceptions; sqlite stores provide in-transaction pointer CAS and ledger append.
- Prior HIGH coverage remains present in tests/test_gap3_durable.py: durable signer fail-closed, promotion/prune/restore rollback on ledger failure, and append-only quarantine restart behavior; no inspected change reintroduced those blockers.

Spec compliance: durable promotion now enforces active_module_cap using the same shared transition plan as in-memory retain, and cap/eviction state is ledgered and atomic in one UoW. Durable active set cannot exceed the cap when approve_promotion is used because the UoW ignores stale caller-supplied new_hashes and computes from the durable pointer under transaction. Eviction is PRUNED, ledgered via evicted_hashes, and reversible via atrophy_and_restore/restore coverage. Mid-tx failure leaves zero partial pointer, lifecycle, or ledger state per rollback test and db.tx semantics.

Architecture: the fix moves cap selection into a shared pure planner, avoiding duplicate in-memory vs durable policy logic. The durable UoW is now the transactional boundary for lifecycle, pointer, and ledger changes. One minor architectural caveat remains outside the target blocker: durable atrophy_and_restore still performs manual cap eviction rather than the shared transition plan, but the target restore test covers cap preservation and this is not a promotion blocker.

Code quality/security/performance: implementation is direct, low allocation, and keeps mutation under SQLite transaction. Ledger payload preserves prior_hashes, new_hashes, and evicted_hashes for audit/restore diagnostics. No security regression observed in signer fail-closed or quarantine paths.

## Root Cause
The prior blocker was policy divergence: durable promotion accepted a caller-computed active set and did not apply the same active_module_cap eviction logic that in-memory retain used, allowing durable active pointers to grow past the cap and leaving eviction state outside the atomic promotion ledger. The fix makes the durable UoW compute the shared plan inside the transaction.

## Findings
No HIGH or blocker findings remain.

LOW — src/ultron/app/triage.py:391-402 — durable atrophy_and_restore restore eviction uses a small manual cap loop instead of plan_active_set_transition. Impact is limited because this is restore, not promotion, and tests verify the cap and lifecycle. Fix suggestion: in a follow-up cleanup, route durable restore cap eviction through the same shared transition planner to remove the last policy duplicate.

## Recommendations
1. Approve the blocker fix.
2. Keep the new parity and rollback tests as regression gates.
3. Optionally refactor durable restore cap handling to use plan_active_set_transition for full policy consolidation.

## Architectural Status
CLEAR

## Code Review Recommendation
APPROVE

## Trade-offs
- Shared plan in both in-memory retain and durable promote: best correctness and low coupling; adopted.
- Let callers pass capped new_hashes: simpler signature but unsafe under concurrency and source of prior divergence; avoided.
- Manual restore eviction loop: acceptable short-term because outside target promotion blocker and tested, but less maintainable than planner reuse.
