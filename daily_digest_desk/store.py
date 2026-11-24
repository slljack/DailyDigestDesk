"""SQLite store for events and readings: idempotent inserts plus per-key fetch cursors.

Public surface:
    Store(path)
    store.add(events)                     -> list[Event]    (only the new ones)
    store.events_inserted_since(since)    -> list[Event]    (stored after `since`)
    store.add_readings(readings)          -> list[Reading]  (only the new ones)
    store.readings_since(series, since)   -> list[Reading]
    store.get_cursor(key) / set_cursor(key, value)
    store.events_to_summarize(since, kinds) -> list[Event]  (no summary yet, newest first)
    store.add_summary(event_id, summary, model=..., prompt_version=..., input_tokens=..., output_tokens=...)
    store.summaries_for(event_ids)        -> dict[str, Summary]  (event id -> its summary)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path

from daily_digest_desk.models import Event, Reading
from daily_digest_desk.reasoning.summarize import Summary

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

CREATE TABLE IF NOT EXISTS summaries (
    event_id       TEXT PRIMARY KEY,
    summary        TEXT NOT NULL,
    key_numbers    TEXT NOT NULL,   -- JSON
    model          TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    inserted_at    TEXT NOT NULL,
    changes        TEXT NOT NULL DEFAULT '[]'   -- JSON: the "key detail" bullets (prompt v3)
);
"""

# Columns added after a table was first created: CREATE TABLE IF NOT EXISTS leaves
# an existing table as it is, so Store adds these to an older database when it
# opens it. Each goes at the end, where a fresh table has it too.
_ADDED_COLUMNS = [
    ("summaries", "changes", "TEXT NOT NULL DEFAULT '[]'"),  # Nov 22, 2026: prompt v3's bullets
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# The columns an Event is built from, in the order _row_to_event reads them.
# inserted_at isn't one: it's the store's bookkeeping, not part of what happened.
_EVENT_COLUMNS = "id, source, kind, ticker, occurred_at, title, url, fields"


def _row_to_event(r: tuple) -> Event:
    return Event(
        id=r[0], source=r[1], kind=r[2], ticker=r[3],
        occurred_at=datetime.fromisoformat(r[4]),
        title=r[5], url=r[6], fields=json.loads(r[7]),
    )


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Bring an older database up to _SCHEMA. Old rows get the column's default:
        a summary saved before v3 gets '[]', no bullets. Safe to run on every open."""
        with self._conn:
            for table, column, definition in _ADDED_COLUMNS:
                have = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

    def events_inserted_since(self, since: datetime) -> list[Event]:
        """Events stored after `since`, oldest stored first. For "new since the last digest".

        Filters on inserted_at (when we stored it), not occurred_at (when it happened):
        an earnings result filled in two days late is still news to us. Strictly after,
        so an event stored at exactly `since` was already on the last digest.
        """
        sql = (
            f"SELECT {_EVENT_COLUMNS} FROM events "
            "WHERE inserted_at > ? "
            "ORDER BY inserted_at, id"  # id breaks ties: one add() batch shares an inserted_at
        )
        return [_row_to_event(r) for r in self._conn.execute(sql, [_iso(since)])]

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

    def events_to_summarize(self, since: datetime, kinds: Iterable[str]) -> list[Event]:
        """Events of these kinds, filed at or after `since`, with no summary yet. Newest first.

        Newest first because the run's cost cap may stop partway: today's filings
        matter most on the page, and the older ones get their turn next run.
        """
        kinds = list(kinds)
        placeholders = ", ".join("?" * len(kinds))  # one ? per kind: "?, ?, ?"
        sql = (
            f"SELECT {_EVENT_COLUMNS} FROM events "
            f"WHERE occurred_at >= ? AND kind IN ({placeholders}) "
            "AND NOT EXISTS (SELECT 1 FROM summaries WHERE summaries.event_id = events.id) "
            "ORDER BY occurred_at DESC, id"
        )
        return [_row_to_event(r) for r in self._conn.execute(sql, [_iso(since), *kinds])]

    def add_summary(
        self,
        event_id: str,
        summary: Summary,
        *,
        model: str,
        prompt_version: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Save a summary that passed its checks. One per event: a second summary
        for the same event replaces the first (say, after a prompt change)."""
        key_numbers = json.dumps([kn.model_dump() for kn in summary.key_numbers])
        with self._conn:
            # Columns named, not counted: a column added later goes at the end.
            self._conn.execute(
                "INSERT OR REPLACE INTO summaries (event_id, summary, key_numbers, model, "
                "prompt_version, input_tokens, output_tokens, inserted_at, changes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id, summary.summary, key_numbers, model, prompt_version,
                    input_tokens, output_tokens, _iso(datetime.now(timezone.utc)),
                    json.dumps(summary.changes),
                ),
            )

    def summaries_for(self, event_ids: Iterable[str]) -> dict[str, Summary]:
        """The saved summaries of these events, keyed by event id, for the page.

        An event with no summary is simply missing from the dict, so the page
        can ask summaries.get(event.id) and get None.
        """
        event_ids = list(event_ids)
        placeholders = ", ".join("?" * len(event_ids))
        sql = (
            "SELECT event_id, summary, key_numbers, changes FROM summaries "
            f"WHERE event_id IN ({placeholders})"
        )
        return {
            event_id: Summary(
                summary=text, key_numbers=json.loads(key_numbers), changes=json.loads(changes),
            )
            for event_id, text, key_numbers, changes in self._conn.execute(sql, event_ids)
        }
