"""Collect new events for the portfolio into the event store.

    desk-collect                                  # holdings.toml -> data/events.db
    desk-collect --holdings my.toml --lookback-days 30

Each ticker has its own cursor (`edgar:<TICKER>`), the last date it was
fetched through. The next run re-fetches from that date inclusive; the overlap
is harmless because the store dedupes on event id. A ticker added to holdings
later starts with a fresh lookback window instead of inheriting others' cursor.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Iterable
from datetime import date, timedelta

from dotenv import load_dotenv

from daily_digest_desk.events import Event
from daily_digest_desk.holdings import load_holdings
from daily_digest_desk.ingestion.edgar_events import fetch_edgar_events
from daily_digest_desk.store import EventStore

Fetcher = Callable[[Iterable[str], date], tuple[list[Event], dict[str, str]]]


def collect_edgar(
    store: EventStore,
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


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Collect new EDGAR events for your holdings.")
    parser.add_argument("--holdings", default="holdings.toml")
    parser.add_argument("--db", default="data/events.db")
    parser.add_argument(
        "--lookback-days", type=int, default=7,
        help="Window for tickers with no cursor yet (default: 7).",
    )
    args = parser.parse_args()

    tickers = [h.ticker for h in load_holdings(args.holdings)]
    with EventStore(args.db) as store:
        new, errors = collect_edgar(store, tickers, date.today(), args.lookback_days)

    for e in sorted(new, key=lambda e: e.occurred_at):
        print(f"{e.occurred_at:%Y-%m-%d %H:%M}Z  {e.title}\n    {e.url}")
    print(f"# {len(new)} new event(s)")
    for ticker, msg in errors.items():
        print(f"# ERROR {ticker}: {msg}")


if __name__ == "__main__":
    main()
