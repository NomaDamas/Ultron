## Summary
GAP3 is not ready to approve. SQLite persistence is real for the main durable flow and the promote unit-of-work uses one SQLite transaction, but key safety requirements remain unmet: production signing can still use a source-controlled dev key, durable prune and restore bypass the ledgered unit-of-work, and the SQLite ledger mutates historical rows for quarantine.

## Analysis
Durability is substantially implemented. `src/ultron/persistence/db.py:14-70` opens a SQLite database, enables foreign keys, attempts WAL, wraps writes in `BEGIN IMMEDIATE`, and runs idempotent v1 schema creation. `src/ultron/persistence/sqlite_stores.py:20-175` persists blobs and modules with content-hash collision checks, and `sqlite_stores.py:180-295` persists active pointers, ledger entries, feedback, and evaluated candidates. `tests/test_gap3_durable.py:25-45` proves restart persistence for registry, pointer, ledger, evaluated-candidate evidence, and blobs across a fresh durable app instance.

Atomic promote is implemented at the immediate unit-of-work boundary. `src/ultron/persistence/unit_of_work.py:20-47` updates lifecycle, performs pointer CAS, and appends a `POINTER_TRANSITION` ledger entry inside one `Database.tx`; `tests/test_gap3_durable.py:57-73` proves stale CAS rolls back lifecycle, pointer, and ledger count. However, product durable prune and restore do not use this boundary: `src/ultron/app/triage.py:410-418` calls `EvolutionLoop.prune` and `EvolutionLoop.restore`, while `src/ultron/evolution/loop.py:115-158` performs pointer swaps and lifecycle updates as separate operations and does not append ledger entries. `PromotionUnitOfWork.prune` and `restore` exist at `unit_of_work.py:23-27`, but the durable app path does not call them.

Append-only and immutability are mixed. Blob and module content-addressed writes are protected by primary keys and collision checks in `src/ultron/persistence/sqlite_stores.py:31-58` and `sqlite_stores.py:97-125`. Ledger appends use `INSERT` in `sqlite_stores.py:213-224`, but quarantine uses `UPDATE ledger SET quarantined = 1` in `sqlite_stores.py:232-235`, which violates the stated no UPDATE/DELETE historical-row requirement for the SQLite ledger.

Manifest signing is only partially fail-closed. `src/ultron/run/signer.py:31-53` makes `EnvKeyProvider` fail when no secret exists, and `signer.py:58-62` rejects the wrong key id. `tests/test_gap3_durable.py:108-123` covers env failure, key-id recording, verification, wrong-key rejection, and tamper rejection. But `src/ultron/run/manifest.py:16` still defines a source-controlled `DEFAULT_RUN_MANIFEST_SIGNING_KEY`, `manifest.py:68-81` still signs and verifies with that default when no signer is supplied, and `src/ultron/app/triage.py:487-516` makes the durable app default to a fixture signer backed by the same dev key. There is no production guard that requires a real provider-sourced key.

The in-memory default path remains the default `TriageApp` path and the recorded QA artifact reports `230 passed, 3 skipped` in `artifacts/gap3-qa.txt`. Durable promotion mirrors evidence gating through `TriageApp.approve_promotion` before invoking the unit-of-work at `src/ultron/app/triage.py:363-391`, but durable prune/restore and ledger append-only semantics do not mirror the required invariants.

WAL and migrations are acceptable for an initial v1 store but carry watch risk. `db.py:24-32` attempts WAL best-effort and `db.py:65-120` creates versioned schema tables idempotently and rejects newer schemas. `tests/test_gap3_durable.py:48-54` covers two-connection stale CAS, but no test asserts the journal mode, lock retry behavior, feedback restart, or mid-failure rollback after pointer update but before ledger append.

## Root Cause
GAP3 added durable store implementations but left two critical contracts optional instead of enforcing them at product boundaries. Signing has a fail-closed provider class, but the manifest and durable app still expose default dev-key signing; lifecycle transitions have a transaction helper, but only promotion is wired through it while prune and restore still use the older non-ledgered evolution loop path.

## Findings
1. HIGH - `src/ultron/run/manifest.py:16`, `src/ultron/run/manifest.py:68-81`, `src/ultron/app/triage.py:487-516` - Production can still sign manifests with a source-controlled default key by omitting a signer, and the durable factory installs a fixture signer by default. Impact: the manifest signing requirement is not fail-closed for production. Fix: remove default-key signing from production APIs, require an explicit `ManifestSigner` or environment-backed provider for durable/production construction, and keep fixture keys only in tests or an explicit dev-only factory.
2. HIGH - `src/ultron/app/triage.py:410-418`, `src/ultron/evolution/loop.py:115-158`, `src/ultron/persistence/unit_of_work.py:23-27` - Durable prune and restore bypass `PromotionUnitOfWork` and are neither ledgered nor atomic as one pointer plus lifecycle plus ledger transaction. Impact: stale CAS or a mid-operation failure can leave unledgered or half-applied durable state, and STATE-02 is satisfied only for promote. Fix: route durable prune and restore through a transaction boundary that performs lifecycle, pointer CAS, and ledger append together; add stale and injected-failure tests for promote, prune, and restore.
3. HIGH - `src/ultron/persistence/sqlite_stores.py:213-235` - SQLite ledger quarantine mutates existing ledger rows with `UPDATE`, despite the explicit append-only and no UPDATE/DELETE historical-row requirement. Impact: the audit trail cannot prove the original row state and the quarantine decision as separate immutable facts. Fix: append quarantine/reversal ledger events or use a separate append-only quarantine table and derive promotability without updating historical ledger rows.
4. MEDIUM - `tests/test_gap3_durable.py:25-45`, `src/ultron/persistence/sqlite_stores.py:242-275` - Feedback has a SQLite store, but the restart test never submits and reloads feedback. Impact: STATE-01 feedback durability is implemented but not proved by the GAP3 tests. Fix: add a restart test that calls `submit_feedback`, reopens the app, and reads the event through `events_for_candidate` or another durable query.
5. MEDIUM - `tests/test_gap3_durable.py:57-73`, `src/ultron/persistence/unit_of_work.py:31-47` - Atomic rollback testing covers stale CAS only, not failure after pointer update and before ledger append. Impact: the most important mid-transaction rollback path is inferred from SQLite behavior rather than protected by regression coverage. Fix: inject a duplicate ledger entry id or failing ledger append inside the transaction and assert pointer, lifecycle, and ledger remain unchanged.
6. LOW - `src/ultron/persistence/db.py:24-32`, `src/ultron/persistence/db.py:65-120` - WAL is best-effort and migrations are v1 idempotent, but there is no assertion/test of actual journal mode or future stepwise migration behavior. Impact: low current risk, higher upgrade risk. Fix: assert WAL for file-backed DBs where supported and add migration-version tests before introducing v2.

## Recommendations
1. Block GAP3 approval until production manifest signing cannot fall back to the source-controlled dev key.
2. Centralize all durable lifecycle transitions in a ledgered SQLite unit-of-work and wire product prune and restore through it.
3. Replace mutable ledger quarantine with an append-only quarantine event model.
4. Add missing restart and rollback regression tests for feedback durability, prune/restore atomicity, and mid-transaction failure.
5. Keep the current blob/module immutable storage and promote transaction structure; those pieces are directionally correct.

## Architectural Status
BLOCK

## Code Review Recommendation
REQUEST CHANGES

## Trade-offs
- Require explicit production signer: strongest fail-closed behavior and simplest audit story; tests need a fixture-only construction path.
- Keep default signer with documentation: lowest churn, but it leaves production one omitted argument away from shared-key signing and does not satisfy the requirement.
- Event-sourced quarantine: immutable audit trail and clean append-only semantics; queries become slightly more complex because promotability must join or fold quarantine events.
- Mutable quarantine flag: simple query path, but it violates the stated ledger integrity model.
- Transaction helper for all transitions: clearer durable boundary and testability; requires EvolutionLoop or TriageApp to separate transition planning from transition commit.
