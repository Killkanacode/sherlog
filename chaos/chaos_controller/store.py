"""Ground-truth storage for injected faults and scenario runs (SQLite).

Only the chaos controller reads or writes this; Sherlog has no route to it.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    scenario_id  TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    ground_truth TEXT NOT NULL,
    status       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS faults (
    id          TEXT PRIMARY KEY,
    run_id      TEXT,
    type        TEXT NOT NULL,
    target      TEXT NOT NULL,
    params      TEXT NOT NULL,
    undo        TEXT NOT NULL,
    status      TEXT NOT NULL,
    injected_at TEXT NOT NULL,
    reverted_at TEXT,
    error       TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _exec(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            self._db.commit()
        return [self._decode(dict(row)) for row in rows]

    @staticmethod
    def _decode(row: dict) -> dict:
        for key in ("params", "undo", "ground_truth"):
            if key in row and row[key] is not None:
                row[key] = json.loads(row[key])
        return row

    # faults
    def add_fault(self, fault_id, run_id, type_, target, params, undo, status, error=None) -> None:
        self._exec(
            "INSERT INTO faults (id, run_id, type, target, params, undo, status, injected_at, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fault_id, run_id, type_, target, json.dumps(params), json.dumps(undo), status, now(), error),
        )

    def fault(self, fault_id: str) -> dict | None:
        rows = self._exec("SELECT * FROM faults WHERE id = ?", (fault_id,))
        return rows[0] if rows else None

    def faults(self, status: str | None = None, run_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM faults WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if run_id:
            sql += " AND run_id = ?"
            args.append(run_id)
        return self._exec(sql + " ORDER BY injected_at", tuple(args))

    def mark_fault(self, fault_id: str, status: str, error: str | None = None) -> None:
        self._exec(
            "UPDATE faults SET status = ?, reverted_at = ?, error = COALESCE(?, error) WHERE id = ?",
            (status, now(), error, fault_id),
        )

    # runs
    def add_run(self, run_id: str, scenario_id: str, ground_truth: dict) -> None:
        self._exec(
            "INSERT INTO runs (id, scenario_id, started_at, ground_truth, status) VALUES (?, ?, ?, ?, 'active')",
            (run_id, scenario_id, now(), json.dumps(ground_truth)),
        )

    def runs(self, limit: int = 50) -> list[dict]:
        return self._exec("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))

    def run(self, run_id: str) -> dict | None:
        rows = self._exec("SELECT * FROM runs WHERE id = ?", (run_id,))
        return rows[0] if rows else None

    def end_active_runs(self) -> None:
        self._exec("UPDATE runs SET status = 'ended', ended_at = ? WHERE status = 'active'", (now(),))
