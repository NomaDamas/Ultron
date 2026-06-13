## Summary
The three requested HIGH fixes are materially resolved for fail closed signing, durable atomic transition writes, and append only quarantine. I cannot approve the overall GAP3 state because durable promotion still bypasses the existing EvolutionLoop active set invariants and can exceed the configured durable active_module_cap without pruning or ledgering evictions.

## Analysis
Fail closed signing is implemented in `src/ultron/run/manifest.py:66-79`: `RunManifest.sign` and `verify` require an explicit `ManifestSigner`, preserve `key_id` in the canonical payload, and return false for missing signatures or keys. `src/ultron/run/signer.py:31-53` resolves production material only through `EnvKeyProvider` and raises when `ULTRON_RUN_MANIFEST_SIGNING_SECRET` is absent. `src/ultron/app/triage.py:476-484` makes durable production construction use `EnvKeyProvider` unless a caller supplied signer is passed, while the source controlled fixture key is reachable only through `build_durable_triage_app_for_tests`; the in memory `TriageApp` default fixture path is unchanged as expected.

Durable prune and restore are now routed through `PromotionUnitOfWork` in `src/ultron/app/triage.py:389-403`, and durable promotion uses it at `src/ultron/app/triage.py:348-358`. The unit of work at `src/ultron/persistence/unit_of_work.py:30-58` places lifecycle updates, active pointer CAS, and `POINTER_TRANSITION` ledger append in one `Database.tx` scope. The test suite covers stale CAS rollback for promotion in `tests/test_gap3_durable.py:86-104` and injected mid transaction ledger failure rollback for promote, prune, and restore in `tests/test_gap3_durable.py:117-144`.

Append only quarantine is implemented by `src/ultron/persistence/sqlite_stores.py:224-256`, where quarantine state is derived from `ledger_quarantine_events`; `mark_quarantined` inserts an event and `promotable_entries` filters by the derived set. The in memory ledger mirrors this derivation in `src/ultron/ledger/side_effect_ledger.py:54-84`. The restart test in `tests/test_gap3_durable.py:163-175` confirms historical ledger rows remain `quarantined = 0`, one quarantine event is appended, and entries reload as quarantined after restart.

The medium and low tests are present: feedback restart in `tests/test_gap3_durable.py:41-61`, WAL and future schema rejection in `tests/test_gap3_durable.py:75-83`, and manifest fail closed roundtrip in `tests/test_gap3_durable.py:200-213`. The inspected QA artifact `artifacts/gap3-qa.txt` reports 234 passed and 3 skipped.

## Root Cause
The remaining blocker is that durable promotion moved persistence writes into a unit of work but did not move or reapply the in memory `EvolutionLoop.retain` invariants. `src/ultron/app/triage.py:348-358` only appends the candidate to `active` and calls `PromotionUnitOfWork.promote`; `src/ultron/persistence/unit_of_work.py:20-27` has no promotion evictions or active cap input. In contrast, `src/ultron/evolution/loop.py:67-90` enforces stale pointer checks, active module cap, eviction selection, prune validation, lifecycle updates for evicted modules, and promotion cooldown state before returning.

## Findings
- HIGH: `src/ultron/app/triage.py:348-358` and `src/ultron/persistence/unit_of_work.py:20-58` leave durable promotion unable to mirror `EvolutionLoop.retain`. Impact: after a second durable promotion, the active set can grow beyond `active_module_cap = 2`, evicted modules are not marked `PRUNED`, and no prune side of the pointer transition is represented in the durable ledger. Fix: compute the same `new_active` and evicted set as `EvolutionLoop.retain`, validate prunes, and pass evicted hashes into a single promotion unit of work so candidate survivor, evicted pruned lifecycles, pointer CAS, and ledger payload commit atomically. Add a durable test that promotes two candidates and asserts cap, evicted lifecycle, and ledger contents.

## Recommendations
1. Extend durable promotion to preserve the in memory active set invariants, including active cap eviction and evicted lifecycle changes, inside `PromotionUnitOfWork`.
2. Add tests for durable promotion cap eviction and for stale CAS rollback on prune and restore, even though the shared `_transition` implementation strongly suggests rollback correctness.
3. Keep the current signing, quarantine, WAL, migration, feedback restart, and mid transaction rollback fixes.

## Architectural Status
`BLOCK`

## Code Review Recommendation
`REQUEST CHANGES`

## Trade-offs
- Reusing `EvolutionLoop.retain` directly preserves behavior but needs an injectable transaction boundary or a persistence adapter that can ledger inside the same transaction.
- Duplicating retain logic in durable promotion is faster to patch but risks future divergence. A helper that calculates the transition plan separately from storage side effects is the safer long term shape.
