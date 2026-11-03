from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from incident_desk.models import Run


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    body TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def save(self, run: Run) -> None:
        body = run.model_dump_json()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs (id, incident_id, status, updated_at, body)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    incident_id = excluded.incident_id,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    body = excluded.body
                """,
                (run.id, run.incident_id, run.status.value, run.updated_at, body),
            )
            self._conn.commit()

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            row = self._conn.execute("SELECT body FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return Run.model_validate_json(row["body"])

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, incident_id, status, updated_at FROM runs ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]
