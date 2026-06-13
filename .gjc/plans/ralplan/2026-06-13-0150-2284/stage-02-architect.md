## Summary
The two reported GAP2 blockers are resolved in the inspected product path: strict registries with a BlobStore now reject placeholder, missing, and hash-mismatched artifact refs, and the G007 canary path creates and registers a blob-backed PromptPack rather than carrying the submitted text as a fake hash. BlobStore kind/type enforcement is present. Remaining risk is test coverage depth for fail-closed missing-parent blob and non-prompt primitives, but the architecture no longer has a blocking product-path bypass.

## Analysis
- Strict registry: `src/ultron/registry/store.py:53-57` makes `allow_unbacked_refs` an explicit constructor flag defaulting to `False`; `store.py:131-147` returns only when `blob_store is None`, skips only `None` refs, rejects non-SHA refs unless the explicit escape hatch is enabled, rejects missing SHA refs, and detects stored-content hash mismatch.
- Blob kind/type: `src/ultron/module/blobs.py:67-85` maps each `BlobKind` to its model and raises `TypeError` before storing mismatched blob models; `get_typed` also enforces read-side type expectations.
- Blob-backed canary: `src/ultron/evolution/variation.py:165-182` loads the parent `PromptPack`, mutates a slot, writes a new PromptPack blob, and returns its SHA as `prompt_pack_hash`; `variation.py:230-234` fail-closes when the parent artifact is missing from the BlobStore. Tool/UI/budget/safety edit paths similarly load parent blobs and write real blobs (`variation.py:184-228`).
- Canary path: `src/ultron/app/triage.py:109-147` seeds baseline through `HarnessModule.create_with_blobs`; `triage.py:237-262` stages candidate creation through the blob-aware engine and then registers the resulting candidate in the strict registry. `src/ultron/app/server.py:82-86` still passes request text under `prompt_pack_hash`, but that value is now interpreted by the variation engine as new prompt text, not retained as the artifact ref.
- Tests: `tests/test_gap2_blobs.py:83-103` covers complete/missing/placeholder/mismatched registry refs; `tests/test_gap2_redteam.py:178-206` keeps legacy placeholder acceptance restricted to `ModuleRegistry()` without a BlobStore and verifies strict rejection with a BlobStore; `tests/test_gap2_blobs.py:144-157` and `tests/test_g007_app.py:20-27` assert canary PromptPack blobs exist and hash-match. The recorded full suite is `224 passed, 3 skipped` in `artifacts/gap2-qa.txt`.

## Root Cause
The original blocker was a boundary violation: artifact-reference fields were allowed to carry arbitrary semantic strings that looked like hashes/placeholders, so module identity and registry registration could appear content-addressed without a stored, verifiable artifact. The fix moves enforcement to the registry boundary and realizes variation changes into stored blobs before registration on the G007 path.

## Findings
No CRITICAL or HIGH findings remain.

- MEDIUM: `src/ultron/evolution/variation.py:230-234`, tests. Fail-closed parent-blob behavior is implemented but not directly tested for missing parent blobs after a BlobStore-backed registry already contains a malformed parent. Add a focused test that removes/tampers a parent blob and asserts `propose/apply` fail before candidate registration.
- LOW: `src/ultron/evolution/variation.py:184-228`, tests. Product code realizes ToolPolicy/UI/Budget/Safety blobs, but tests only deeply assert the prompt path and BlobStore kind/type. Add one parametrized test for these primitives to protect the non-prompt blob-backed paths.

## Recommendations
1. Approve GAP2 after the blocker fixes; no remaining blocker was found in the inspected product path.
2. Add follow-up regression tests for missing parent blob and the non-prompt blob-realization paths to reduce future drift risk.
3. Keep legacy tests on `ModuleRegistry()`/`blob_store=None`; do not re-enable placeholder acceptance in BlobStore-backed product registries.

## Architectural Status
CLEAR

## Code Review Recommendation
APPROVE

## Trade-offs
- Strict BlobStore-backed registry: maximizes correctness and auditability; requires legacy fixtures to opt out explicitly with `blob_store=None` or `allow_unbacked_refs=True`.
- Compatibility escape hatch: preserves old in-memory tests/fixtures; safe because it is not default-on and not used by the product `TriageApp` path.
- Staging registry via deepcopy: keeps canary trial isolated until live adapter validation; acceptable because final registration rechecks the candidate against the strict real registry.
