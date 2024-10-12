"""SQLite-backed event store."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Iterable, Iterator

from .models import Event

# Columns that may be pushed down into SQL as case-insensitive equality filters.
PUSHDOWN_COLUMNS = ("index", "host", "source", "sourcetype", "service", "level")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    idx TEXT NOT NULL,
    host TEXT NOT NULL,
    source TEXT NOT NULL,
    sourcetype TEXT NOT NULL,
    service TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    fields TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_idx_ts ON events (idx, ts);
"""


class EventStore:
    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            import os

            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)

    def insert_many(self, events: Iterable[Event]) -> int:
        rows = [
            (e.ts, e.index, e.host, e.source, e.sourcetype, e.service, e.level,
             e.message, json.dumps(e.fields, separators=(",", ":"), default=str))
            for e in events
        ]
        with self._lock:
            self._db.executemany(
                "INSERT INTO events (ts, idx, host, source, sourcetype, service, level, message, fields)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self._db.commit()
        return len(rows)

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        return Event(
            id=r["id"], ts=r["ts"], index=r["idx"], host=r["host"], source=r["source"],
            sourcetype=r["sourcetype"], service=r["service"], level=r["level"],
            message=r["message"], fields=json.loads(r["fields"]),
        ).to_row()

    def query(
        self,
        earliest: float | None = None,
        latest: float | None = None,
        equals: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield event rows (newest first) in [earliest, latest)."""
        sql = ["SELECT * FROM events WHERE 1=1"]
        args: list[Any] = []
        if earliest is not None:
            sql.append("AND ts >= ?")
            args.append(earliest)
        if latest is not None:
            sql.append("AND ts < ?")
            args.append(latest)
        for col, val in (equals or {}).items():
            if col not in PUSHDOWN_COLUMNS:
                continue
            sql.append(f"AND {'idx' if col == 'index' else col} = ? COLLATE NOCASE")
            args.append(val)
        sql.append("ORDER BY ts DESC, id DESC")
        if limit is not None:
            sql.append("LIMIT ?")
            args.append(limit)
        with self._lock:
            fetched = self._db.execute(" ".join(sql), args).fetchall()
        for r in fetched:
            yield self._row(r)

    def get(self, event_id: int) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._row(r) if r else None

    def stats(self) -> dict[str, Any]:
        with self._lock:
            n, lo, hi = self._db.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM events").fetchone()
            idx = [r[0] for r in self._db.execute("SELECT DISTINCT idx FROM events ORDER BY idx")]
        return {"events": n, "earliest": lo, "latest": hi, "indexes": idx}

    def delete_index(self, index: str) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM events WHERE idx = ?", (index,))
            self._db.commit()
            return cur.rowcount
