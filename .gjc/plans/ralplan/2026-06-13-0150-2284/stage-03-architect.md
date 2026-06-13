## Summary
GAP2 implements real canonical content addressing for the blob models and the baseline seed path is blob-backed. Approval is blocked because the registry still accepts non-64-hex legacy artifact references whenever a BlobStore is present, and current product canary paths still create new modules through that bypass.

## Analysis
Content addressing is real for the new blob primitives. src/ultron/module/blobs.py:24-33 serializes model JSON with sort_keys=True and hashes canonical bytes with sha256. src/ultron/module/blobs.py:73-83 keys BlobStore entries by kind and content hash, treats repeated puts of equivalent content as idempotent, guards same-hash byte collisions, deep-copies on put, and deep-copies on get.

Blob-backed module construction is wired. src/ultron/module/model.py:123-142 writes prompt, tool, UI, safety, and budget blobs before assigning the five hash fields, and src/ultron/module/model.py:144-152 returns those references for registry verification. src/ultron/app/triage.py:51-53 constructs one BlobStore-backed registry, and src/ultron/app/triage.py:109-134 seeds the baseline through HarnessModule.create_with_blobs.

The strict registry path works only for sha-shaped values. src/ultron/registry/store.py:132-143 rejects missing 64-hex references and rehashes stored blob content to detect mismatches. tests/test_gap2_blobs.py:80-93 covers the complete, missing, and tampered 64-hex cases.

The compatibility carve-out is too broad. src/ultron/registry/store.py:136-137 skips every non-64-hex reference instead of limiting old placeholders to a fixture-only or no-BlobStore path. This is observable in current product flows: src/ultron/app/server.py:84-85 generates canaries with placeholder prompt_pack_hash values, src/ultron/app/triage.py:233-253 registers those candidates through the blob-backed registry, and tests/test_g007_app.py:19 and :37 expect candidate-good and candidate-bad to register.

Module identity remains deterministic and excludes runtime fitness. src/ultron/module/model.py:93-111 excludes content_hash and fitness from identity_fields, dumps JSON mode, sorts keys, and hashes deterministic JSON. tests/test_gap2_blobs.py:96-110 checks stable module hashes across stores and unchanged identity after a fitness change.

## Root Cause
Legacy scalar placeholder compatibility is implemented as a silent skip in the production blob verification boundary. That makes content-addressed verification optional for any new module that chooses a non-sha string.

## Findings
1. HIGH - src/ultron/registry/store.py:132-143 - Non-64-hex artifact references bypass verification even when the registry has a BlobStore. Impact: blob-backed modules can register forged or unmaterialized prompt, tool, UI, safety, or budget refs. Fix: reject non-sha refs by default when blob_store is present; keep legacy only behind an explicit test or migration mode, or only for registries without BlobStore.
2. HIGH - src/ultron/app/server.py:84-85 and src/ultron/app/triage.py:233-253 - New canary/product modules still use scalar placeholder prompt_pack_hash values. Impact: strict GAP2 enforcement would break current product paths, proving the bypass is active outside old fixtures. Fix: create real PromptPack blobs for candidate prompt edits or require variation payloads to supply blob content that is stored before registration.
3. MEDIUM - src/ultron/module/blobs.py:73-83 - BlobStore does not enforce a BlobKind to blob model type mapping, and registry verification does not call get_typed. Impact: the hash can be valid while the stored blob type is wrong for the referenced field. Fix: enforce kind/type compatibility on put or in registry verification.
4. LOW - tests/test_gap2_blobs.py:80-93 - Tests do not cover non-sha rejection under ModuleRegistry(BlobStore()). Impact: the principal bypass remains normalized by older G007 tests. Fix: add regression coverage for rejecting candidate-good style refs in a blob-backed registry unless explicit legacy mode is enabled.

## Recommendations
1. Block GAP2 approval until blob-backed registries reject non-sha artifact refs by default.
2. Update variation and server canary generation to store real blobs, not placeholder scalar hashes.
3. Make the legacy bridge explicit and bounded to old fixtures or migration-only registries.
4. Add kind/type enforcement for blobs before treating typed artifacts as a hard product invariant.

## Architectural Status
BLOCK

## Product Status
BLOCK

## Code Status
BLOCK

## Code Review Recommendation
REQUEST CHANGES

## Trade-offs
- Strict rejection gives a clear content-addressed invariant and catches regressions early, but requires updating legacy tests and canary payload shape.
- Explicit legacy mode preserves older placeholder fixtures with low churn, but production TriageApp and server paths must never enable it.
- Keeping the current skip avoids churn but defeats the core GAP2 guarantee for new blob-backed modules.
