"""SQLite event store: idempotent inserts plus per-key fetch cursors.

Public surface:
    EventStore(path)
    store.add(events)                     -> list[Event]  (only the new ones)
    store.events_since(since, tickers)    -> list[Event]
    store.get_cursor(key) / set_cursor(key, value)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from daily_digest_desk.events import Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    ticker      TEXT,
    occurred_at TEXT NOT NULL,   -- ISO-8601 UTC, so string order == time order
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    fields      TEXT NOT NULL,   -- JSON
    inserted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_occurred_at ON events(occurred_at);
CREATE INDEX IF NOT EXISTS events_ticker ON events(ticker);

CREATE TABLE IF NOT EXISTS cursors (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class EventStore:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> EventStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def add(self, events: Iterable[Event]) -> list[Event]:
        """Insert events, skipping ids already stored. Returns only the new ones.

        Duplicates within the same batch are also skipped (first one wins).
        """
        now = _iso(datetime.now(timezone.utc))
        new: list[Event] = []
        with self._conn:  # one transaction for the batch
            for e in events:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        e.id, e.source, e.kind, e.ticker, _iso(e.occurred_at),
                        e.title, e.url, json.dumps(e.fields, default=str), now,
                    ),
                )
                if cur.rowcount == 1:
                    new.append(e)
        return new

    def events_since(
        self,
        since: datetime,
        tickers: Iterable[str] | None = None,
    ) -> list[Event]:
        """Events with occurred_at >= since, oldest first. Optionally filter by ticker."""
        sql = (
            "SELECT id, source, kind, ticker, occurred_at, title, url, fields "
            "FROM events WHERE occurred_at >= ?"
        )
        params: list[str] = [_iso(since)]
        if tickers is not None:
            tickers = list(tickers)
            sql += f" AND ticker IN ({','.join('?' * len(tickers))})"
            params.extend(tickers)
        sql += " ORDER BY occurred_at, id"
        return [
            Event(
                id=r[0], source=r[1], kind=r[2], ticker=r[3],
                occurred_at=datetime.fromisoformat(r[4]),
                title=r[5], url=r[6], fields=json.loads(r[7]),
            )
            for r in self._conn.execute(sql, params)
        ]

    def get_cursor(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM cursors WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_cursor(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO cursors (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
