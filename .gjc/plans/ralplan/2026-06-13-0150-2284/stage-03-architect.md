## Summary
GAP7 is not ready to approve. The implementation adds useful primitives for sessions, scopes, telemetry, and durable actor columns, but the privileged-action boundary is split inconsistently, rollback actor attribution is missing, and the committed Hermes integrity manifest is not a real hash manifest.

## Analysis
- Auth/session evidence: src/ultron/auth/principal.py:12-61 defines Scope, expiring SessionStore.resolve, and cookie attributes with HttpOnly, SameSite=strict, and configurable Secure. src/ultron/app/server.py:25-94 resolves the session and applies ACTION_SCOPES before calling the legacy CSRF/pointer/policy validator.
- Auth gate gap: src/ultron/app/server.py:25-31 includes RUN_BENCHMARK in ACTION_SCOPES, but src/ultron/ui/runtime.py:36-41 omits RUN_BENCHMARK from PRIVILEGED_ACTIONS, and validate_action returns early for actions not in that set. As a result, benchmark evidence creation is scope-gated but not CSRF-, pointer-, or policy-gated. tests/test_gap7_platform.py:80-92 currently posts RUN_BENCHMARK without CSRF/header/pointer and expects 200.
- Existing policy/evidence gates mostly remain intact: src/ultron/app/triage.py:392-415 still requires benchmark-runner provenance, fixture id, trajectory ids, promotable evidence label, and promotable selector outcome before promotion; src/ultron/app/server.py:124-137 routes approval through approve_promotion after auth/scope/validator checks.
- Actor audit evidence: src/ultron/run/manifest.py:60-61 and 108-139 carries actor in signed manifests; src/ultron/persistence/db.py:82-94 migrates an actor column; src/ultron/persistence/sqlite_stores.py:203-218 writes LedgerEntry.actor; and src/ultron/persistence/unit_of_work.py:21-100 requires actor for durable promote/prune/restore and stores it both in the ledger column and payload.
- Actor audit gap: src/ultron/app/server.py:134-141 authenticates rollback and only records the subject in telemetry. src/ultron/ledger/canary_store.py:98-104 performs rollback/quarantine without an actor, while SideEffectLedger.mark_quarantined at src/ultron/ledger/side_effect_ledger.py:62-72 appends an actorless quarantine entry. The SQLite quarantine event table also has no actor field.
- Telemetry: src/ultron/obs/telemetry.py:7-54 exposes declared counters plus typed event records only; src/ultron/app/server.py:71-74 returns that deterministic snapshot. I did not find token/key/secret payload exposure in metrics.
- Vendor integrity: src/ultron/hermes/integrity.py:37-57 is graceful when the vendor tree is absent and fail-closed on missing/listed critical files or hash drift when present. However src/ultron/hermes/hermes_vendor_integrity.json:2-8 contains all-zero SHA-256 values, so the committed manifest is not a real manifest for the pinned Hermes files.
- README: README.md:24-61 is appropriately honest about fake Hermes/model seams, live fail-closed expectations, and GAP1-GAP7 status; it does not overclaim live Hermes/model operation.

## Root Cause
Privileged-action semantics are duplicated in two places: ACTION_SCOPES in the server and PRIVILEGED_ACTIONS in UI runtime validation. GAP7 added benchmark scope gating to the server but did not update the legacy validator, so defense-in-depth gates diverged. Actor attribution likewise stops at promotion/prune/restore UOWs and was not threaded through rollback/quarantine.

## Findings
1. HIGH -- src/ultron/ui/runtime.py:36-41: RUN_BENCHMARK is scope-gated but bypasses CSRF/pointer/policy validation. Impact: a scoped session can mutate benchmark evidence/telemetry without the same defense-in-depth gates used by other privileged actions. Fix: add RUN_BENCHMARK to PRIVILEGED_ACTIONS or unify privileged-action metadata, then update tests to require CSRF/header/current pointer.
2. HIGH -- src/ultron/ledger/canary_store.py:98-104: rollback/quarantine mutation has no actor. Impact: a destructive rollback can occur without durable actor attribution even though the API resolved a principal. Fix: pass actor through RollbackController.rollback and mark_quarantined, and persist it in the quarantine ledger/event schema with tests for API and durable paths.
3. HIGH -- src/ultron/hermes/hermes_vendor_integrity.json:2-8: committed critical-file hashes are placeholders. Impact: vendor-present verification cannot prove the pinned reference is intact; a correct checkout would fail like a corrupted tree. Fix: regenerate from the real pinned vendored files and make manifest generation fail if a critical file is missing.
4. MEDIUM -- src/ultron/app/triage.py:300-324: propose_and_canary creates candidate run manifests/ledger entries with actor=None, including when called from SUBMIT_REQUEST. Impact: non-privileged mutation can be actorless; document this as intentionally anonymous or thread the local principal through submit/canary creation.

## Recommendations
1. Unify action metadata so scope, CSRF, active-pointer, and policy requirements cannot drift between server and UI runtime.
2. Make rollback/quarantine actor-aware in both in-memory and SQLite-backed ledgers, with actor in payload/column/event record.
3. Replace the Hermes integrity manifest with real pinned-file SHA-256s and add a positive vendor-present verification test against a generated manifest fixture.
4. Update GAP7 tests to reject no-CSRF RUN_BENCHMARK, assert rollback actor persistence, and assert the manifest does not contain placeholder hashes.

## Architectural Status
BLOCK

## Code Review Recommendation
REQUEST CHANGES

## Trade-offs
- Keeping ACTION_SCOPES and PRIVILEGED_ACTIONS separate is simple locally but fragile; a single action-policy table is safer and makes future privileged actions harder to misclassify.
- Actorless unprivileged submit/canary flow preserves a lightweight local sandbox path, but it should be explicit product scope rather than an accidental audit gap.
- Vendor-absent graceful behavior is appropriate for this sandbox, but it must coexist with a real manifest so vendor-present deployments can be verified rather than always failing.
