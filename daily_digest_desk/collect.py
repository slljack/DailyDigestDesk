"""Collect new events and readings for the portfolio into the store.

    desk-collect                                  # holdings.toml -> data/events.db
    desk-collect --holdings my.toml --lookback-days 30

Each ticker has its own cursor (`edgar:<TICKER>`), the last date it was
fetched through. The next run re-fetches from that date inclusive; the overlap
is harmless because the store dedupes on event id. A ticker added to holdings
later starts with a fresh lookback window instead of inheriting others' cursor.

FRED series also have one cursor each (`fred:<SERIES>`). FRED is skipped
if FRED_API_KEY isn't set.

Finnhub quotes have no cursor: `/quote` only returns the latest price, and a
repeat run's reading is deduped on (series, date). Finnhub earnings have no
cursor either: each run checks a window from a week ago to a week ahead, and
the store dedupes on event id. Finnhub is skipped if FINNHUB_API_KEY isn't set.
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
from collections.abc import Callable, Iterable
from datetime import date, timedelta, datetime, timezone
from typing import Protocol

from dotenv import load_dotenv

from daily_digest_desk.models import Event, Reading
from daily_digest_desk.holdings import load_holdings
from daily_digest_desk.ingestion import fred, finnhub
from daily_digest_desk.ingestion.edgar_events import fetch_edgar_events
from daily_digest_desk.store import Store

log = logging.getLogger(__name__)

Fetcher = Callable[[Iterable[str], date], tuple[list[Event], dict[str, str]]]
FinnhubFetcher = Callable[[str], dict]
EarningsFetcher = Callable[[str, date], list[dict]]


class FredFetcher(Protocol):
    """Anything shaped like fred.fetch_observations with the API key already filled in."""

    def __call__(self, series_id: str, since: date, first_release: bool = False) -> list[dict]: ...


# How far back to look for the prior month, so a release's change can be computed.
_PRIOR_MONTH_WINDOW = timedelta(days=100)


def collect_edgar(
    store: Store,
    tickers: Iterable[str],
    today: date,
    lookback_days: int = 7,
    fetch: Fetcher = fetch_edgar_events,
) -> tuple[list[Event], dict[str, str]]:
    """Fetch, store, and advance cursors. Returns (new events, errors by ticker).

    `fetch` is injectable so tests can run without network.
    """
    new: list[Event] = []
    errors: dict[str, str] = {}
    for ticker in tickers:
        key = f"edgar:{ticker}"
        cursor = store.get_cursor(key)
        since = date.fromisoformat(cursor) if cursor else today - timedelta(days=lookback_days)

        events, errs = fetch([ticker], since)
        if errs:
            # Leave the cursor alone so the next run retries this window.
            errors.update(errs)
            continue
        new.extend(store.add(events))
        store.set_cursor(key, today.isoformat())
    return new, errors


def collect_fred(
    store: Store,
    today: date,
    fetch: FredFetcher,
    lookback_days: int = 30,
) -> tuple[list[Reading], list[Event], dict[str, str]]:
    """Fetch FRED series and store them. Returns (new readings, new events, errors by series).

    `fetch` is injectable so tests can run without network. The default lookback
    is longer than EDGAR's because signal rules compare against last week's values.
    """
    new_readings: list[Reading] = []
    new_events: list[Event] = []
    errors: dict[str, str] = {}

    def since_for(series_id: str) -> date:
        cursor = store.get_cursor(f"fred:{series_id}")
        return date.fromisoformat(cursor) if cursor else today - timedelta(days=lookback_days)

    for series_id in fred.DAILY_SERIES:
        try:
            readings = fred.observations_to_readings(series_id, fetch(series_id, since_for(series_id)))
        except Exception as exc:  # noqa: BLE001 — isolate per-series failures
            log.warning("FRED fetch failed for %s: %s", series_id, exc)
            errors[series_id] = str(exc)
            continue
        new_readings.extend(store.add_readings(readings))
        if readings:
            # Cursor = newest day FRED had, not today: daily values are posted a
            # day or so late, and starting from today next time would skip them.
            store.set_cursor(f"fred:{series_id}", max(r.date for r in readings).isoformat())

    for release in fred.RELEASES.values():
        since = since_for(release.series_id)
        start = since - _PRIOR_MONTH_WINDOW
        try:
            # Two requests: today's values (for the change) and first releases (for the date).
            observations = fetch(release.series_id, start)
            first_releases = fetch(release.series_id, start, first_release=True)
            event = fred.latest_release_event(release, observations, first_releases, since)
        except Exception as exc:  # noqa: BLE001
            log.warning("FRED fetch failed for %s: %s", release.series_id, exc)
            errors[release.series_id] = str(exc)
            continue
        if event:
            new_events.extend(store.add([event]))
        store.set_cursor(f"fred:{release.series_id}", today.isoformat())

    return new_readings, new_events, errors


def collect_finnhub(
    store: Store,
    tickers: Iterable[str],
    now: datetime,
    fetch: FinnhubFetcher,
) -> tuple[list[Reading], dict[str, str]]:
    """Store each ticker's latest close. Returns (new readings, errors by ticker).

    `fetch` is injectable so tests can run without network. During market hours
    nothing is stored, and that isn't an error.
    """
    new_readings: list[Reading] = []
    errors: dict[str, str] = {}

    for ticker in tickers:
        try:
            reading = finnhub.quote_to_reading(ticker, fetch(ticker), now)
        except Exception as exc:  # noqa: BLE001 — isolate per-ticker failures
            log.warning("Finnhub failed for %s: %s", ticker, exc)
            errors[ticker] = str(exc)
            continue
        if reading:
            new_readings.extend(store.add_readings([reading]))

    return new_readings, errors

def collect_finnhub_earnings(
    store: Store,
    tickers: Iterable[str],
    today: date,
    fetch: EarningsFetcher,
) -> tuple[list[Event], dict[str, str]]:
    """Store earnings heads-ups and results. Returns (new events, errors by ticker).

    `fetch` is injectable so tests can run without network. A ticker whose rows
    fail to fetch or map stores nothing, so it's retried whole on the next run.
    """
    new_events: list[Event] = []
    errors: dict[str, str] = {}

    for ticker in tickers:
        try:
            rows = fetch(ticker, today)
            events: list[Event] = []
            for row in rows:
                event = finnhub.earnings_to_event(row, today)
                if event is not None:
                    events.append(event)
        except Exception as exc:  # noqa: BLE001 — isolate per-ticker failures
            log.warning("Finnhub earnings failed for %s: %s", ticker, exc)
            errors[ticker] = str(exc)
            continue
        new_events.extend(store.add(events))

    return new_events, errors


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Collect new EDGAR, FRED and Finnhub data for your holdings.")
    parser.add_argument("--holdings", default="holdings.toml")
    parser.add_argument("--db", default="data/events.db")
    parser.add_argument(
        "--lookback-days", type=int, default=7,
        help="Window for tickers with no cursor yet (default: 7).",
    )
    args = parser.parse_args()

    tickers = [h.ticker for h in load_holdings(args.holdings)]
    fred_key = os.environ.get("FRED_API_KEY")
    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    readings: list[Reading] = []
    finnhub_readings: list[Reading] = []
    with Store(args.db) as store:
        new, errors = collect_edgar(store, tickers, date.today(), args.lookback_days)
        if fred_key:
            fetch = functools.partial(fred.fetch_observations, api_key=fred_key)
            readings, fred_events, fred_errors = collect_fred(store, date.today(), fetch)
            new += fred_events
            errors.update(fred_errors)
        if finnhub_key:
            fetch = functools.partial(finnhub.fetch_quote, api_key=finnhub_key)
            finnhub_readings, finnhub_errors = collect_finnhub(store, tickers, datetime.now(timezone.utc), fetch)
            errors.update(finnhub_errors)

    for e in sorted(new, key=lambda e: e.occurred_at):
        print(f"{e.occurred_at:%Y-%m-%d %H:%M}Z  {e.title}\n    {e.url}")
    print(f"# {len(new)} new event(s)")
    if fred_key:
        print(f"# {len(readings)} new FRED reading(s)")
    else:
        print("# FRED skipped: set FRED_API_KEY in .env")
    if finnhub_key:
        print(f"# {len(finnhub_readings)} new Finnhub reading(s)")
    else:
        print("# FINNHUB skipped: set FINNHUB_API_KEY in .env")
    for key, msg in errors.items():
        print(f"# ERROR {key}: {msg}")


if __name__ == "__main__":
    main()
