"""SQLite-backed stores mirroring the in-memory persistence APIs."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, cast

from ultron.evaluation.harness import EvaluationReport
from ultron.evolution.selection import SelectionOutcome
from ultron.feedback.channel import FeedbackChannel, FeedbackEvent, FeedbackEventType, SourceReliability
from ultron.ledger.side_effect_ledger import LedgerEntry, SideEffectKind, SideEffectLedger
from ultron.module.blobs import _BLOB_TYPES, BlobKind, BlobT, ModuleBlob
from ultron.module.model import HarnessModule
from ultron.persistence.db import Database
from ultron.registry.pointer import ActivePointerStore
from ultron.registry.store import Layer, ModuleLifecycle, RegistryEntry, _is_sha256_hex, _module_identity_bytes, _expands_permissions


def _scope_key(key: tuple[str, str]) -> str:
    return json.dumps(list(key), separators=(",", ":"))


def _loads_key(value: str) -> tuple[str, str]:
    a, b = json.loads(value)
    return (a, b)


class SqliteBlobStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def put(self, kind: BlobKind, blob: ModuleBlob) -> str:
        expected_type = _BLOB_TYPES[kind]
        if not isinstance(blob, expected_type):
            raise TypeError(f"{kind.value} blob must be {expected_type.__name__}, got {type(blob).__name__}")
        content_hash = blob.content_hash()
        content_json = blob.model_dump_json()
        with self.db.tx() as cur:
            row = cur.execute("SELECT content_json FROM blobs WHERE kind = ? AND hash = ?", (kind.value, content_hash)).fetchone()
            if row is not None and row["content_json"] != content_json:
                raise ValueError(f"blob content hash collision for {kind.value}: {content_hash}")
            cur.execute("INSERT OR IGNORE INTO blobs(kind, hash, content_json) VALUES (?, ?, ?)", (kind.value, content_hash, content_json))
        return content_hash

    def get(self, kind: BlobKind, content_hash: str) -> ModuleBlob:
        row = self.db.conn.execute("SELECT content_json FROM blobs WHERE kind = ? AND hash = ?", (kind.value, content_hash)).fetchone()
        if row is None:
            raise KeyError((kind, content_hash))
        return _BLOB_TYPES[kind].model_validate_json(row["content_json"])

    def get_typed(self, kind: BlobKind, content_hash: str, expected_type: type[BlobT]) -> BlobT:
        blob = self.get(kind, content_hash)
        if not isinstance(blob, expected_type):
            raise TypeError(f"blob {content_hash} for {kind.value} is not {expected_type.__name__}")
        return cast(BlobT, blob)

    def has(self, kind: BlobKind, content_hash: str) -> bool:
        row = self.db.conn.execute("SELECT 1 FROM blobs WHERE kind = ? AND hash = ?", (kind.value, content_hash)).fetchone()
        return row is not None


class SqliteModuleRegistry:
    def __init__(self, db: Database, blob_store: SqliteBlobStore | None = None, *, allow_unbacked_refs: bool = False) -> None:
        self.db = db
        self.blob_store = blob_store
        self.allow_unbacked_refs = allow_unbacked_refs

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        from ultron.module.blobs import BlobStore
        from ultron.registry.store import ModuleRegistry
        clone_blob_store = BlobStore()
        if self.blob_store is not None:
            for row in self.db.conn.execute("SELECT kind, hash FROM blobs"):
                kind = BlobKind(row["kind"])
                clone_blob_store.put(kind, self.blob_store.get(kind, row["hash"]))
        clone = ModuleRegistry(clone_blob_store, allow_unbacked_refs=self.allow_unbacked_refs)
        for row in self.db.conn.execute("SELECT content_hash FROM modules ORDER BY version, content_hash"):
            entry = self.get(row["content_hash"])
            clone.register(
                entry.module,
                entry.lifecycle,
                entry.layer,
                consent_ok=entry.consent_ok,
                redacted=entry.redacted,
                human_approved_additive=entry.human_approved_additive,
            )
        return clone

    def register(self, module: HarnessModule, lifecycle: ModuleLifecycle, layer: Layer, *, consent_ok: bool = False, redacted: bool = False, human_approved_additive: bool = False) -> RegistryEntry:
        if layer == "global" and not (consent_ok and redacted):
            raise ValueError("global modules require consent_ok=True and redacted=True")
        self._verify_blob_references(module)
        finalized = module.finalized()
        supplied_hash = module.content_hash or finalized.content_hash
        if supplied_hash is None:
            raise ValueError("finalized module must have content_hash")
        if supplied_hash != finalized.content_hash:
            raise ValueError("content hash does not match module identity bytes")
        module_json = finalized.model_dump_json()
        with self.db.tx() as cur:
            row = cur.execute("SELECT module_json FROM modules WHERE content_hash = ?", (supplied_hash,)).fetchone()
            if row is not None:
                existing = HarnessModule.model_validate_json(row["module_json"])
                if _module_identity_bytes(existing) != _module_identity_bytes(finalized):
                    raise ValueError("content hash collision: existing module bytes differ")
                return self.get(supplied_hash)
            cur.execute("INSERT INTO modules(content_hash, module_json, module_id, version) VALUES (?, ?, ?, ?)", (supplied_hash, module_json, finalized.module_id, finalized.version))
            cur.execute(
                "INSERT INTO module_lifecycle(content_hash, lifecycle, layer, created_at, consent_ok, redacted, human_approved_additive) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (supplied_hash, lifecycle.value, layer, time.time(), int(consent_ok), int(redacted), int(human_approved_additive)),
            )
        return self.get(supplied_hash)

    def get(self, content_hash: str) -> RegistryEntry:
        row = self.db.conn.execute(
            "SELECT m.module_json, l.lifecycle, l.layer, l.created_at, l.consent_ok, l.redacted, l.human_approved_additive FROM modules m JOIN module_lifecycle l USING(content_hash) WHERE m.content_hash = ?",
            (content_hash,),
        ).fetchone()
        if row is None:
            raise KeyError(content_hash)
        return RegistryEntry(module=HarnessModule.model_validate_json(row["module_json"]), lifecycle=ModuleLifecycle(row["lifecycle"]), layer=row["layer"], created_at=row["created_at"], consent_ok=bool(row["consent_ok"]), redacted=bool(row["redacted"]), human_approved_additive=bool(row["human_approved_additive"])).model_copy(deep=True)

    def versions_of(self, module_id: str) -> list[RegistryEntry]:
        rows = self.db.conn.execute("SELECT content_hash FROM modules WHERE module_id = ? ORDER BY version, content_hash", (module_id,)).fetchall()
        return [self.get(row["content_hash"]) for row in rows]

    def lineage(self, content_hash: str) -> list[RegistryEntry]:
        lineage: list[RegistryEntry] = []
        current = self.get(content_hash)
        while True:
            lineage.append(current.model_copy(deep=True))
            parent_hash = current.module.parent_id
            if parent_hash is None:
                return lineage
            current = self.get(parent_hash)

    def set_lifecycle(self, content_hash: str, new_lifecycle: ModuleLifecycle) -> RegistryEntry:
        with self.db.tx() as cur:
            result = cur.execute("UPDATE module_lifecycle SET lifecycle = ? WHERE content_hash = ?", (new_lifecycle.value, content_hash))
            if result.rowcount != 1:
                raise KeyError(content_hash)
        return self.get(content_hash)

    def _verify_blob_references(self, module: HarnessModule) -> None:
        if self.blob_store is None:
            return
        for kind, content_hash in module.referenced_blob_hashes().items():
            if content_hash is None:
                continue
            if not _is_sha256_hex(content_hash):
                if self.allow_unbacked_refs:
                    continue
                raise ValueError(f"artifact ref not blob-backed for {kind.value}: {content_hash}")
            if not self.blob_store.has(kind, content_hash):
                raise ValueError(f"missing blob for {kind.value}: {content_hash}")
            stored = self.blob_store.get(kind, content_hash)
            actual_hash = stored.content_hash()
            if actual_hash != content_hash:
                raise ValueError(f"blob hash mismatch for {kind.value}: expected {content_hash}, got {actual_hash}")

    def can_auto_promote(self, content_hash_or_module: str | HarnessModule) -> bool:
        candidate = content_hash_or_module.finalized() if isinstance(content_hash_or_module, HarnessModule) else self.get(content_hash_or_module).module
        if candidate.parent_id is None:
            return True
        parent = self.get(candidate.parent_id).module
        return not _expands_permissions(candidate, parent)


class SqliteActivePointerStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    @property
    def _pointers(self) -> dict[tuple[str, str], tuple[int, tuple[str, ...]]]:
        rows = self.db.conn.execute("SELECT scope_key, version, hashes_json FROM active_pointer").fetchall()
        return {_loads_key(row["scope_key"]): (row["version"], tuple(json.loads(row["hashes_json"]))) for row in rows}

    def get(self, key: tuple[str, str]) -> tuple[int, list[str]]:
        row = self.db.conn.execute("SELECT version, hashes_json FROM active_pointer WHERE scope_key = ?", (_scope_key(key),)).fetchone()
        if row is None:
            return (0, [])
        return (int(row["version"]), list(json.loads(row["hashes_json"])))

    def swap(self, key: tuple[str, str], expected_version: int, new_hashes: list[str]) -> int:
        with self.db.tx() as cur:
            return self._swap_in_tx(cur, key, expected_version, new_hashes)

    def _swap_in_tx(self, cur: sqlite3.Cursor, key: tuple[str, str], expected_version: int, new_hashes: list[str]) -> int:
        encoded_key = _scope_key(key)
        hashes_json = json.dumps(list(new_hashes), separators=(",", ":"))
        if expected_version == 0:
            try:
                cur.execute("INSERT INTO active_pointer(scope_key, version, hashes_json) VALUES (?, 1, ?)", (encoded_key, hashes_json))
            except sqlite3.IntegrityError as exc:
                raise ValueError("stale active pointer version") from exc
            return 1
        result = cur.execute("UPDATE active_pointer SET version = version + 1, hashes_json = ? WHERE scope_key = ? AND version = ?", (hashes_json, encoded_key, expected_version))
        if result.rowcount != 1:
            raise ValueError("stale active pointer version")
        row = cur.execute("SELECT version FROM active_pointer WHERE scope_key = ?", (encoded_key,)).fetchone()
        return int(row["version"])


class SqliteSideEffectLedger:
    def __init__(self, db: Database) -> None:
        self.db = db

    def append(self, entry: LedgerEntry) -> str:
        with self.db.tx() as cur:
            self._append_in_tx(cur, entry)
        return entry.entry_id

    def _append_in_tx(self, cur: sqlite3.Cursor, entry: LedgerEntry) -> str:
        stored = entry.model_copy(deep=True)
        cur.execute(
            "INSERT INTO ledger(entry_id, run_id, module_set_hash, module_hash, canary_id, kind, payload_json, reversible, non_reversible_marker, quarantined, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (stored.entry_id, stored.run_id, stored.module_set_hash, stored.module_hash, stored.canary_id, stored.kind.value, json.dumps(stored.payload, sort_keys=True, separators=(",", ":")), int(stored.reversible), stored.non_reversible_marker, int(stored.quarantined), stored.created_at),
        )
        return stored.entry_id

    def _from_row(self, row: sqlite3.Row) -> LedgerEntry:
        return LedgerEntry(entry_id=row["entry_id"], run_id=row["run_id"], module_set_hash=row["module_set_hash"], module_hash=row["module_hash"], canary_id=row["canary_id"], kind=SideEffectKind(row["kind"]), payload=json.loads(row["payload_json"]), reversible=bool(row["reversible"]), non_reversible_marker=row["non_reversible_marker"], created_at=row["created_at"], quarantined=bool(row["quarantined"]))

    def entries_for_canary(self, canary_id: str) -> list[LedgerEntry]:
        return [self._from_row(row).model_copy(deep=True) for row in self.db.conn.execute("SELECT * FROM ledger WHERE canary_id = ? ORDER BY created_at, entry_id", (canary_id,))]

    def entries_for_run(self, run_id: str) -> list[LedgerEntry]:
        return [self._from_row(row).model_copy(deep=True) for row in self.db.conn.execute("SELECT * FROM ledger WHERE run_id = ? ORDER BY created_at, entry_id", (run_id,))]

    def mark_quarantined(self, canary_id: str) -> list[str]:
        with self.db.tx() as cur:
            rows = cur.execute("SELECT entry_id FROM ledger WHERE canary_id = ? ORDER BY created_at, entry_id", (canary_id,)).fetchall()
            cur.execute("UPDATE ledger SET quarantined = 1 WHERE canary_id = ?", (canary_id,))
        return [row["entry_id"] for row in rows]

    def promotable_entries(self) -> list[LedgerEntry]:
        return [self._from_row(row).model_copy(deep=True) for row in self.db.conn.execute("SELECT * FROM ledger WHERE quarantined = 0 ORDER BY created_at, entry_id")]


class SqliteFeedbackChannel:
    def __init__(self, db: Database) -> None:
        self.db = db

    def ingest(self, event: FeedbackEvent) -> FeedbackEvent:
        if event.event_type == FeedbackEventType.OUTCOME and event.verifier_id is not None and event.source_reliability == SourceReliability.MODEL_GENERATED:
            raise ValueError("model-generated feedback cannot verify outcomes")
        if event.global_template_eligibility and not self._qualifies_for_global_template(event):
            raise ValueError("global template eligibility requires global-template consent and redaction")
        with self.db.tx() as cur:
            cur.execute("INSERT INTO feedback(event_id, event_json, candidate_id, run_id, event_type, source_reliability, verifier_id, timestamp, retention_rule, global_template_eligibility) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (event.event_id, event.model_dump_json(), event.candidate_id, event.run_id, event.event_type.value, event.source_reliability.value, event.verifier_id, event.timestamp, event.retention_rule, int(event.global_template_eligibility)))
        return event

    def _event_rows(self, where: str = "", params: tuple[Any, ...] = ()) -> list[FeedbackEvent]:
        sql = "SELECT event_json FROM feedback " + where + " ORDER BY timestamp, event_id"
        return [FeedbackEvent.model_validate_json(row["event_json"]) for row in self.db.conn.execute(sql, params)]

    def events_for_candidate(self, candidate_id: str) -> list[FeedbackEvent]:
        return self._event_rows("WHERE candidate_id = ?", (candidate_id,))

    def outcome_verifiers(self) -> list[FeedbackEvent]:
        return [event for event in self._event_rows() if event.can_verify_outcome]

    def purge_expired(self, now: float) -> None:
        thirty_days_seconds = 30 * 24 * 60 * 60
        with self.db.tx() as cur:
            cur.execute("DELETE FROM feedback WHERE retention_rule = 'ephemeral'")
            cur.execute("DELETE FROM feedback WHERE retention_rule = '30d' AND ? - timestamp > ?", (now, thirty_days_seconds))

    def _qualifies_for_global_template(self, event: FeedbackEvent) -> bool:
        return FeedbackChannel()._qualifies_for_global_template(event)

    def global_eligible_events(self) -> list[FeedbackEvent]:
        return [event for event in self._event_rows() if event.global_template_eligibility and self._qualifies_for_global_template(event)]


class SqliteEvaluatedCandidateStore(dict[str, dict[str, Any]]):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db

    def __setitem__(self, candidate_hash: str, value: dict[str, Any]) -> None:
        report = value["report"]
        outcome = value["outcome"]
        canary_id = value.get("canary_id")
        with self.db.tx() as cur:
            cur.execute("INSERT OR REPLACE INTO evaluated_candidates(candidate_hash, report_json, outcome_json, canary_id) VALUES (?, ?, ?, ?)", (candidate_hash, report.model_dump_json(), outcome.model_dump_json(), canary_id))

    def get(self, candidate_hash: str, default: Any = None) -> Any:
        row = self.db.conn.execute("SELECT report_json, outcome_json, canary_id FROM evaluated_candidates WHERE candidate_hash = ?", (candidate_hash,)).fetchone()
        if row is None:
            return default
        return {"report": EvaluationReport.model_validate_json(row["report_json"]), "outcome": SelectionOutcome.model_validate_json(row["outcome_json"]), "canary_id": row["canary_id"]}

    def __contains__(self, candidate_hash: object) -> bool:
        if not isinstance(candidate_hash, str):
            return False
        return self.get(candidate_hash) is not None
