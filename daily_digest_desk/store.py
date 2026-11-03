"""SQLite store for events and readings: idempotent inserts plus per-key fetch cursors.

Public surface:
    Store(path)
    store.add(events)                     -> list[Event]    (only the new ones)
    store.events_since(since, tickers)    -> list[Event]
    store.add_readings(readings)          -> list[Reading]  (only the new ones)
    store.readings_since(series, since)   -> list[Reading]
    store.get_cursor(key) / set_cursor(key, value)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path

from daily_digest_desk.models import Event, Reading

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

-- One row per (series, date). The primary key also serves as the index for
-- "series X since date Y", so no extra index is needed.
CREATE TABLE IF NOT EXISTS readings (
    series      TEXT NOT NULL,   -- e.g. 'fred:DGS10', 'finnhub:NVDA'
    date        TEXT NOT NULL,   -- calendar day 'YYYY-MM-DD'; string order == date order
    value       REAL NOT NULL,
    inserted_at TEXT NOT NULL,
    PRIMARY KEY (series, date)
);

CREATE TABLE IF NOT EXISTS cursors (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
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

    def add_readings(self, readings: Iterable[Reading]) -> list[Reading]:
        """Insert readings, skipping (series, date) pairs already stored. Returns only the new ones.

        First value wins: if a source later revises a day's value, the stored one is kept.
        """
        now = _iso(datetime.now(timezone.utc))
        new: list[Reading] = []
        with self._conn:
            for r in readings:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO readings VALUES (?, ?, ?, ?)",
                    (r.series, r.date.isoformat(), r.value, now),
                )
                if cur.rowcount == 1:
                    new.append(r)
        return new

    def readings_since(self, series: str, since: date) -> list[Reading]:
        """Readings for one series with date >= since, oldest first."""
        rows = self._conn.execute(
            "SELECT series, date, value FROM readings "
            "WHERE series = ? AND date >= ? ORDER BY date",
            (series, since.isoformat()),
        )
        return [
            Reading(series=r[0], date=date.fromisoformat(r[1]), value=r[2])
            for r in rows
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
