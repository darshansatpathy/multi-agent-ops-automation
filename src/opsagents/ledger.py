"""The run ledger: every tool call, every agent decision, every approval.

Three uses:
- idempotency: a write that already succeeded under the same key is not sent again,
  across retries, crashes, resumes and resubmitted tickets;
- deterministic replay: when a graph node re-runs (LangGraph re-executes a node from
  its start after an interrupt), each agent decision is read back instead of asked again;
- audit: the evaluation's policy auditor reads it to judge every side effect.

Backends: in memory, SQLite and PostgreSQL, with the same schema.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol


@dataclass
class LedgerEntry:
    run_id: str
    call_id: str
    worker: str
    tool: str
    kind: str  # read | write
    args: dict[str, Any]
    status: str  # ok | error | rejected | approval_required
    result: Any = None
    error: str | None = None
    error_kind: str | None = None
    attempts: int = 1
    at: float = 0.0
    seq: int = 0


class LedgerStore(Protocol):
    def append(self, entry: LedgerEntry) -> LedgerEntry: ...
    def ok_result(self, call_id: str) -> LedgerEntry | None: ...
    def entries(self, run_id: str | None = None) -> list[LedgerEntry]: ...
    def get_decision(self, run_id: str, key: str) -> dict[str, Any] | None: ...
    def put_decision(self, run_id: str, key: str, action: dict[str, Any]) -> None: ...
    def get_approval(self, fingerprint: str) -> dict[str, Any] | None: ...
    def put_approval(self, fingerprint: str, decision: dict[str, Any]) -> None: ...
    def close(self) -> None: ...


class InMemoryLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[LedgerEntry] = []
        self._decisions: dict[tuple[str, str], dict[str, Any]] = {}
        self._approvals: dict[str, dict[str, Any]] = {}

    def append(self, entry: LedgerEntry) -> LedgerEntry:
        with self._lock:
            entry.seq = len(self._entries) + 1
            entry.at = entry.at or time.time()
            self._entries.append(entry)
            return entry

    def ok_result(self, call_id: str) -> LedgerEntry | None:
        with self._lock:
            return next((e for e in reversed(self._entries) if e.call_id == call_id and e.status == "ok"), None)

    def entries(self, run_id: str | None = None) -> list[LedgerEntry]:
        with self._lock:
            return [e for e in self._entries if run_id is None or e.run_id == run_id]

    def get_decision(self, run_id: str, key: str) -> dict[str, Any] | None:
        return self._decisions.get((run_id, key))

    def put_decision(self, run_id: str, key: str, action: dict[str, Any]) -> None:
        self._decisions.setdefault((run_id, key), action)

    def get_approval(self, fingerprint: str) -> dict[str, Any] | None:
        return self._approvals.get(fingerprint)

    def put_approval(self, fingerprint: str, decision: dict[str, Any]) -> None:
        self._approvals.setdefault(fingerprint, decision)

    def close(self) -> None:
        return None


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS ops_ledger (
        seq {serial}, run_id TEXT NOT NULL, call_id TEXT NOT NULL, worker TEXT NOT NULL,
        tool TEXT NOT NULL, kind TEXT NOT NULL, args TEXT NOT NULL, status TEXT NOT NULL,
        result TEXT, error TEXT, error_kind TEXT, attempts INTEGER NOT NULL, at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ops_ledger_call ON ops_ledger (call_id, status)",
    "CREATE INDEX IF NOT EXISTS ops_ledger_run ON ops_ledger (run_id)",
    """CREATE TABLE IF NOT EXISTS ops_decisions (
        run_id TEXT NOT NULL, key TEXT NOT NULL, action TEXT NOT NULL, PRIMARY KEY (run_id, key))""",
    """CREATE TABLE IF NOT EXISTS ops_approvals (
        fingerprint TEXT PRIMARY KEY, decision TEXT NOT NULL)""",
]
_SELECT_BY_CALL = (
    "SELECT seq, run_id, call_id, worker, tool, kind, args, status, result, error, error_kind, attempts, at "
    "FROM ops_ledger WHERE call_id = ? AND status = 'ok' ORDER BY seq DESC LIMIT 1"
)
_SELECT_ALL = (
    "SELECT seq, run_id, call_id, worker, tool, kind, args, status, result, error, error_kind, attempts, at "
    "FROM ops_ledger ORDER BY seq"
)
_SELECT_RUN = (
    "SELECT seq, run_id, call_id, worker, tool, kind, args, status, result, error, error_kind, attempts, at "
    "FROM ops_ledger WHERE run_id = ? ORDER BY seq"
)


class SqlLedger:
    """SQLite or PostgreSQL (psycopg 3). Placeholders are written as `?` and adapted."""

    def __init__(self, conn: Any, dialect: str) -> None:
        self._conn = conn
        self._dialect = dialect
        self._lock = threading.Lock()
        serial = "INTEGER PRIMARY KEY AUTOINCREMENT" if dialect == "sqlite" else "BIGSERIAL PRIMARY KEY"
        with self._lock:
            for stmt in _SCHEMA:
                self._conn.execute(stmt.format(serial=serial))
            self._conn.commit()

    @classmethod
    def sqlite(cls, path: str = ":memory:") -> SqlLedger:
        return cls(sqlite3.connect(path, check_same_thread=False), "sqlite")

    @classmethod
    def postgres(cls, dsn: str) -> SqlLedger:
        import psycopg

        return cls(psycopg.connect(dsn), "postgres")

    def _sql(self, sql: str) -> str:
        return sql if self._dialect == "sqlite" else sql.replace("?", "%s")

    def _one(self, sql: str, params: tuple[Any, ...]) -> Any:
        with self._lock:
            cur = self._conn.execute(self._sql(sql), params)
            return cur.fetchone()

    def append(self, entry: LedgerEntry) -> LedgerEntry:
        entry.at = entry.at or time.time()
        values = (
            entry.run_id,
            entry.call_id,
            entry.worker,
            entry.tool,
            entry.kind,
            json.dumps(entry.args),
            entry.status,
            json.dumps(entry.result),
            entry.error,
            entry.error_kind,
            entry.attempts,
            entry.at,
        )
        with self._lock:
            cur = self._conn.execute(
                self._sql(
                    "INSERT INTO ops_ledger (run_id, call_id, worker, tool, kind, args, status, result, error,"
                    " error_kind, attempts, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING seq"
                ),
                values,
            )
            entry.seq = int(cur.fetchone()[0])
            self._conn.commit()
        return entry

    @staticmethod
    def _row(row: Any) -> LedgerEntry:
        seq, run_id, call_id, worker, tool, kind, args, status, result, error, error_kind, attempts, at = row
        return LedgerEntry(
            run_id,
            call_id,
            worker,
            tool,
            kind,
            json.loads(args),
            status,
            json.loads(result) if result is not None else None,
            error,
            error_kind,
            int(attempts),
            float(at),
            int(seq),
        )

    def ok_result(self, call_id: str) -> LedgerEntry | None:
        row = self._one(_SELECT_BY_CALL, (call_id,))
        return self._row(row) if row else None

    def entries(self, run_id: str | None = None) -> list[LedgerEntry]:
        with self._lock:
            if run_id is None:
                cur = self._conn.execute(_SELECT_ALL)
            else:
                cur = self._conn.execute(self._sql(_SELECT_RUN), (run_id,))
            return [self._row(r) for r in cur.fetchall()]

    def get_decision(self, run_id: str, key: str) -> dict[str, Any] | None:
        row = self._one("SELECT action FROM ops_decisions WHERE run_id = ? AND key = ?", (run_id, key))
        return json.loads(row[0]) if row else None

    def put_decision(self, run_id: str, key: str, action: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                self._sql(
                    "INSERT INTO ops_decisions (run_id, key, action) VALUES (?, ?, ?)"
                    " ON CONFLICT (run_id, key) DO NOTHING"
                ),
                (run_id, key, json.dumps(action)),
            )
            self._conn.commit()

    def get_approval(self, fingerprint: str) -> dict[str, Any] | None:
        row = self._one("SELECT decision FROM ops_approvals WHERE fingerprint = ?", (fingerprint,))
        return json.loads(row[0]) if row else None

    def put_approval(self, fingerprint: str, decision: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                self._sql(
                    "INSERT INTO ops_approvals (fingerprint, decision) VALUES (?, ?)"
                    " ON CONFLICT (fingerprint) DO NOTHING"
                ),
                (fingerprint, json.dumps(decision)),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def entry_dict(entry: LedgerEntry) -> dict[str, Any]:
    return asdict(entry)
