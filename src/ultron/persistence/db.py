"""SQLite persistence primitives and schema migrations."""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Iterator

SCHEMA_VERSION = 2


class Database:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = RLock()
        self._tx_depth = 0
        self._tx_cursor: sqlite3.Cursor | None = None
        self._configure()
        migrate(self)

    def _configure(self) -> None:
        self.conn.execute("PRAGMA foreign_keys=ON")
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            if self._tx_depth > 0:
                assert self._tx_cursor is not None
                self._tx_depth += 1
                try:
                    yield self._tx_cursor
                except Exception:
                    raise
                finally:
                    self._tx_depth -= 1
                return

            cur = self.conn.cursor()
            self._tx_cursor = cur
            self._tx_depth = 1
            try:
                cur.execute("BEGIN IMMEDIATE")
                yield cur
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            finally:
                self._tx_depth = 0
                self._tx_cursor = None
                cur.close()


def migrate(db: Database) -> None:
    with db.tx() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS schema_meta (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)")
        row = cur.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
        if row is not None and int(row["version"]) > SCHEMA_VERSION:
            raise RuntimeError(f"database schema version {row['version']} is newer than supported {SCHEMA_VERSION}")
        cur.execute("INSERT OR IGNORE INTO schema_meta (id, version) VALUES (1, 0)")
        cur.execute("CREATE TABLE IF NOT EXISTS blobs (kind TEXT NOT NULL, hash TEXT NOT NULL, content_json TEXT NOT NULL, PRIMARY KEY(kind, hash))")
        cur.execute("CREATE TABLE IF NOT EXISTS modules (content_hash TEXT PRIMARY KEY, module_json TEXT NOT NULL, module_id TEXT NOT NULL, version INTEGER NOT NULL)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_modules_id_version ON modules(module_id, version, content_hash)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS module_lifecycle (
                content_hash TEXT PRIMARY KEY,
                lifecycle TEXT NOT NULL,
                layer TEXT NOT NULL,
                created_at REAL NOT NULL,
                consent_ok INTEGER NOT NULL DEFAULT 0,
                redacted INTEGER NOT NULL DEFAULT 0,
                human_approved_additive INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(content_hash) REFERENCES modules(content_hash)
            )
        """)
        cur.execute("CREATE TABLE IF NOT EXISTS active_pointer (scope_key TEXT PRIMARY KEY, version INTEGER NOT NULL, hashes_json TEXT NOT NULL)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ledger (
                entry_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                module_set_hash TEXT NOT NULL,
                module_hash TEXT,
                canary_id TEXT,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                reversible INTEGER NOT NULL,
                non_reversible_marker TEXT,
                quarantined INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        columns = {row["name"] for row in cur.execute("PRAGMA table_info(ledger)").fetchall()}
        if "actor" not in columns:
            cur.execute("ALTER TABLE ledger ADD COLUMN actor TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_canary ON ledger(canary_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_run ON ledger(run_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ledger_quarantine_events (
                event_id TEXT PRIMARY KEY,
                canary_id TEXT NOT NULL,
                entry_ids_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_quarantine_canary ON ledger_quarantine_events(canary_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                event_id TEXT PRIMARY KEY,
                event_json TEXT NOT NULL,
                candidate_id TEXT,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source_reliability TEXT NOT NULL,
                verifier_id TEXT,
                timestamp REAL NOT NULL,
                retention_rule TEXT NOT NULL,
                global_template_eligibility INTEGER NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_candidate ON feedback(candidate_id)")
        cur.execute("CREATE TABLE IF NOT EXISTS evaluated_candidates (candidate_hash TEXT PRIMARY KEY, report_json TEXT NOT NULL, outcome_json TEXT NOT NULL, canary_id TEXT)")
        cur.execute("UPDATE schema_meta SET version = ? WHERE id = 1", (SCHEMA_VERSION,))
