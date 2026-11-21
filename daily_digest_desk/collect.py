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
repeat run's reading is deduped on (series, date). Quotes are fetched for every
holding plus SPY, the S&P 500 benchmark. Finnhub earnings have no
cursor either: each run checks a window from a week ago to a week ahead, and
the store dedupes on event id. Finnhub is skipped if FINNHUB_API_KEY isn't set.

Signal rules run last, on the readings this run added: a big move from the
previous reading becomes an Event. Only readings from the past week are checked,
so a first run's backfill doesn't report old moves as news.

Then filings from the past week with no summary yet (8-K, 6-K, 13D, S-3, 424B)
get a plain-English summary from Claude, checked against the filing, newest
first, up to SUMMARY_CAP_DOLLARS a run. Skipped if ANTHROPIC_API_KEY isn't set.
A filing that fails is tried again next run.

Each run ends by writing a digest page, data/digests/<New York date-time>.html,
with every event stored since the last page (the `digest:last` cursor; the first
page covers the past week). Open it in a browser.

A failure in one ticker or series doesn't stop the run. Anything that failed is
listed as `# ERROR ...` at the end, and the exit code is 1.
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv

from daily_digest_desk import signals
from daily_digest_desk.holdings import load_holdings
from daily_digest_desk.ingestion import finnhub, fred
from daily_digest_desk.ingestion.edgar_events import fetch_edgar_events
from daily_digest_desk.ingestion.edgar_text import fetch_filing_documents, trim_documents
from daily_digest_desk.models import Event, Reading
from daily_digest_desk.output.digest import build_digest
from daily_digest_desk.reasoning.summarize import (
    PROMPT_VERSION, SUMMARY_FORMS, SummaryError, SummaryResult, dollars, summarize_checked,
)
from daily_digest_desk.store import Store

log = logging.getLogger(__name__)

Fetcher = Callable[[Iterable[str], date], tuple[list[Event], dict[str, str]]]
FinnhubFetcher = Callable[[str], dict]
EarningsFetcher = Callable[[str, date], list[dict]]
DigestBuilder = Callable[[list[Event], datetime], str]  # (events, now) -> HTML page
DocumentFetcher = Callable[[str, str], list[tuple[str, str]]]  # (accession, form) -> documents
Summarizer = Callable[[Event, dict[str, str], object], SummaryResult]  # (event, documents, client)
# collect_summaries with the client filled in: called as (store, now=...).
SummaryCollector = Callable[..., "tuple[SummaryRun, dict[str, str]]"]


class FredFetcher(Protocol):
    """Anything shaped like fred.fetch_observations with the API key already filled in."""

    def __call__(self, series_id: str, since: date, first_release: bool = False) -> list[dict]: ...


# How far back to look for the prior month, so a release's change can be computed.
_PRIOR_MONTH_WINDOW = timedelta(days=100)

# How far back to look for the reading before a new one. Long weekends and bond-market
# holidays are up to 4 days; the rest is room for a day FRED has no value.
_PREVIOUS_READING_WINDOW = timedelta(days=10)

# Only readings this recent are checked for signals. A first run backfills weeks of
# FRED history, and a big move from three weeks ago isn't news. Older readings are
# still stored and can still be the previous reading for a newer one.
_SIGNAL_RECENT_WINDOW = timedelta(days=7)

# The digest's bookmark: when the last page was written.
_DIGEST_CURSOR = "digest:last"
_FIRST_DIGEST_WINDOW = timedelta(days=7)  # what the very first page covers

_NEW_YORK = ZoneInfo("America/New_York")

# Filings older than this don't get a summary: they're no longer news on the page,
# and a filing that fails every day stops costing money after a week.
_SUMMARY_WINDOW = timedelta(days=7)

# The most one run spends on summaries (DESIGN.md section 7). A fuse, not a budget:
# the busiest day so far is about $1 even if every summary needs its retry.
SUMMARY_CAP_DOLLARS = 2.00


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
            observations = fetch(series_id, since_for(series_id))
            readings = fred.observations_to_readings(series_id, observations)
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


def collect_signals(
    store: Store,
    readings: Iterable[Reading],
    rules: Iterable[signals.BigMove],
    today: date,
) -> tuple[list[Event], dict[str, str]]:
    """Check each new reading against its rule. Returns (new events, errors by reading).

    Run it after the other collectors have stored their readings, so a reading
    stored earlier in the same run can be the previous one (a backlog of Thu and
    Fri compares Fri with Thu). Skipped, not errors: a reading older than a week
    (first-run backfill), a reading with no rule (a series kept only as history),
    and one with no earlier reading in the window (a holding's first day).
    """
    rules_by_series = {rule.series: rule for rule in rules}
    new_events: list[Event] = []
    errors: dict[str, str] = {}

    for latest in readings:
        if latest.date < today - _SIGNAL_RECENT_WINDOW:
            continue
        rule = rules_by_series.get(latest.series)
        if rule is None:
            continue
        try:
            since = latest.date - _PREVIOUS_READING_WINDOW
            earlier = [
                r for r in store.readings_since(latest.series, since) if r.date < latest.date
            ]
            if not earlier:
                continue
            # readings_since is oldest first, so [-1] is the newest one before `latest`
            event = signals.big_move_event(rule, earlier[-1], latest)
        except Exception as exc:  # noqa: BLE001 — isolate per-reading failures
            log.warning("Signal check failed for %s %s: %s", latest.series, latest.date, exc)
            errors[f"{latest.series} {latest.date}"] = str(exc)
            continue
        if event is not None:
            new_events.extend(store.add([event]))

    return new_events, errors


@dataclass
class SummaryRun:
    """What collect_summaries did: for the run's report."""

    saved: list[str]  # event ids that got a summary
    left: int         # filings not tried because the cap was reached; the next run gets them
    dollars: float    # estimated spend, failed calls included


def collect_summaries(
    store: Store,
    client,
    now: datetime,
    cap: float = SUMMARY_CAP_DOLLARS,
    fetch: DocumentFetcher = fetch_filing_documents,
    summarize: Summarizer = summarize_checked,
) -> tuple[SummaryRun, dict[str, str]]:
    """Summarize recent filings that have none yet. Returns (what it did, errors by event id).

    Newest first, until `cap` dollars are spent. The cap is checked before each
    filing, so the run can go over it by at most one summary. A filing that fails
    (no text, fetch error, model refused, failed its checks twice) gets no row,
    so the next run tries it again, until it's older than _SUMMARY_WINDOW.
    `fetch` and `summarize` are injectable so tests can run with no network.
    """
    to_do = store.events_to_summarize(now - _SUMMARY_WINDOW, SUMMARY_FORMS)
    saved: list[str] = []
    errors: dict[str, str] = {}
    spent = 0.0
    left = 0

    for i, event in enumerate(to_do):
        if spent >= cap:
            left = len(to_do) - i
            break
        try:
            documents = trim_documents(fetch(event.fields["accession_number"], event.kind))
            if not any(documents.values()):
                # Nothing to read (an exhibit that's only pictures): don't pay to ask.
                errors[event.id] = "no text in the filing to summarize"
                continue
            result = summarize(event, documents, client)
        except SummaryError as exc:
            spent += dollars(exc.input_tokens, exc.output_tokens)  # a failed call still costs
            log.warning("Summary failed for %s: %s", event.id, exc)
            errors[event.id] = str(exc)
            continue
        except Exception as exc:  # noqa: BLE001 — fetch failed, API down: isolate per filing
            log.warning("Summary failed for %s: %s", event.id, exc)
            errors[event.id] = str(exc)
            continue

        spent += dollars(result.input_tokens, result.output_tokens)
        store.add_summary(
            event.id, result.summary,
            model=result.model, prompt_version=PROMPT_VERSION,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        )
        saved.append(event.id)

    return SummaryRun(saved, left, spent), errors


def write_digest(
    store: Store,
    now: datetime,
    out_dir: Path,
    build: DigestBuilder,
) -> Path:
    """Write a page of the events stored since the last digest. Returns the file's path.

    `now` should be taken just before this call. The cursor (`digest:last`) is set
    to it, so an event stored during the call lands on two pages at worst, never on
    none. The cursor moves only after the file is written: if building or writing
    fails, the next digest shows the same events again. With no cursor yet (the
    first digest), the page covers the past week. `build` is injectable so tests
    don't depend on the page's HTML.
    """
    cursor = store.get_cursor(_DIGEST_CURSOR)
    since = datetime.fromisoformat(cursor) if cursor else now - _FIRST_DIGEST_WINDOW
    page = build(store.events_inserted_since(since), now)

    out_dir.mkdir(parents=True, exist_ok=True)
    # New York time in the name, with seconds, so a rerun in the same minute
    # doesn't overwrite the page before it: data/digests/2026-11-17-080012.html
    path = out_dir / f"{now.astimezone(_NEW_YORK):%Y-%m-%d-%H%M%S}.html"
    path.write_text(page, encoding="utf-8")
    # The same page under a fixed name, so one browser bookmark always opens the newest.
    (out_dir / "latest.html").write_text(page, encoding="utf-8")

    # UTC ISO text, the same format as the store's inserted_at, so `>` compares right.
    store.set_cursor(_DIGEST_CURSOR, now.astimezone(timezone.utc).isoformat())
    return path


def new_york_today(now: datetime) -> date:
    """The date in New York, whatever the Mac's time zone. Filings, prices and
    earnings are all dated in New York, so a 10pm run in Denver isn't a day behind."""
    return now.astimezone(_NEW_YORK).date()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Sources:
    """The fetchers one run uses. None skips that source (its API key isn't set)."""

    edgar: Fetcher = fetch_edgar_events
    fred: FredFetcher | None = None
    quote: FinnhubFetcher | None = None
    earnings: EarningsFetcher | None = None
    summaries: SummaryCollector | None = None


@dataclass
class RunResult:
    new: list[Event]  # every new event: filings, releases, earnings, signals
    fred_readings: list[Reading]
    quote_readings: list[Reading]
    earnings_events: list[Event]
    signal_events: list[Event]
    digest_path: Path | None  # None if the page failed
    errors: dict[str, dict[str, str]]  # source -> {ticker, series or "page": message}
    summaries: SummaryRun | None = None  # None if skipped (no key) or it failed


def run(
    store: Store,
    tickers: list[str],
    today: date,
    sources: Sources,
    digest_dir: Path,
    lookback_days: int = 7,
    clock: Callable[[], datetime] = _utc_now,
) -> RunResult:
    """One desk-collect run: every collector, then signals, then the digest page.

    Takes its fetchers and clock as arguments, so tests can run it with fakes and
    no network. The clock is read when each step needs the time, not once at the
    start: the page's cursor must be later than everything this run stored.
    """
    errors: dict[str, dict[str, str]] = {}
    new, errors["edgar"] = collect_edgar(store, tickers, today, lookback_days, sources.edgar)

    fred_readings: list[Reading] = []
    if sources.fred:
        fred_readings, fred_events, errors["fred"] = collect_fred(store, today, sources.fred)
        new += fred_events

    quote_readings: list[Reading] = []
    if sources.quote:
        # The benchmark's close too, for the S&P 500 rule; dict.fromkeys drops it
        # if it's also a holding.
        quote_symbols = list(dict.fromkeys([*tickers, signals.BENCHMARK]))
        quote_readings, errors["finnhub_quotes"] = collect_finnhub(
            store, quote_symbols, clock(), sources.quote,
        )

    earnings_events: list[Event] = []
    if sources.earnings:
        earnings_events, errors["finnhub_earnings"] = collect_finnhub_earnings(
            store, tickers, today, sources.earnings,
        )
        new += earnings_events

    # After the collectors, so this run's readings are already in the store.
    rules = signals.MACRO_RULES + signals.stock_rules(tickers)
    signal_events, errors["signals"] = collect_signals(
        store, fred_readings + quote_readings, rules, today,
    )
    new += signal_events

    # After the collectors, so today's filings are in the store, and before the
    # page, so it can show them. A crash here (the store, not one filing: those
    # are caught inside) is an error, and the page still comes out.
    summary_run: SummaryRun | None = None
    if sources.summaries:
        try:
            summary_run, errors["summaries"] = sources.summaries(store, now=clock())
        except Exception as exc:  # noqa: BLE001 — the page must still be written
            log.warning("Summaries failed: %s", exc)
            errors["summaries"] = {"run": str(exc)}

    # Last, so the page has everything this run stored. A failed page leaves the
    # digest cursor alone, so the next run's page shows the same events again.
    digest_path: Path | None = None
    build = functools.partial(build_digest, tickers=tickers)
    try:
        digest_path = write_digest(store, clock(), digest_dir, build)
        errors["digest"] = {}
    except Exception as exc:  # noqa: BLE001 — the collected data is already stored
        log.warning("Digest page failed: %s", exc)
        errors["digest"] = {"page": str(exc)}

    return RunResult(
        new, fred_readings, quote_readings, earnings_events, signal_events, digest_path, errors,
        summary_run,
    )


def report_lines(result: RunResult, sources: Sources) -> list[str]:
    """What desk-collect prints: new events, counts, the page, then any errors."""
    lines = []
    for e in sorted(result.new, key=lambda e: e.occurred_at):
        lines.append(f"{e.occurred_at:%Y-%m-%d %H:%M}Z  {e.title}\n    {e.url}")
    lines.append(f"# {len(result.new)} new event(s)")
    if sources.fred:
        lines.append(f"# {len(result.fred_readings)} new FRED reading(s)")
    else:
        lines.append("# FRED skipped: set FRED_API_KEY in .env")
    if sources.quote:
        lines.append(f"# {len(result.quote_readings)} new Finnhub quote reading(s)")
        lines.append(f"# {len(result.earnings_events)} new Finnhub earnings event(s), included above")
    else:
        lines.append("# Finnhub skipped: set FINNHUB_API_KEY in .env")
    lines.append(f"# {len(result.signal_events)} new signal event(s), included above")
    if not sources.summaries:
        lines.append("# Summaries skipped: set ANTHROPIC_API_KEY in .env")
    elif result.summaries:  # None if the step itself failed: its error is listed below
        s = result.summaries
        lines.append(f"# {len(s.saved)} new summary(ies), about ${s.dollars:.2f}")
        if s.left:
            lines.append(f"# Summary cap reached: {s.left} filing(s) left for the next run")
    if result.digest_path:
        lines.append(f"# Digest page: {result.digest_path.resolve()}")
    for source, source_errors in result.errors.items():
        for name, msg in source_errors.items():
            lines.append(f"# ERROR {source} {name}: {msg}")
    return lines


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Collect new EDGAR, FRED and Finnhub data for your holdings.",
    )
    parser.add_argument("--holdings", default="holdings.toml")
    parser.add_argument("--db", default="data/events.db")
    parser.add_argument(
        "--lookback-days", type=int, default=7,
        help="Window for tickers with no cursor yet (default: 7).",
    )
    parser.add_argument(
        "--digest-dir", default="data/digests",
        help="Where each run's digest page is written (default: data/digests).",
    )
    args = parser.parse_args()

    tickers = [h.ticker for h in load_holdings(args.holdings)]
    fred_key = os.environ.get("FRED_API_KEY")
    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    sources = Sources(
        fred=functools.partial(fred.fetch_observations, api_key=fred_key) if fred_key else None,
        quote=functools.partial(finnhub.fetch_quote, api_key=finnhub_key) if finnhub_key else None,
        earnings=(
            functools.partial(finnhub.fetch_earnings, api_key=finnhub_key) if finnhub_key else None
        ),
        summaries=(
            functools.partial(collect_summaries, client=anthropic.Anthropic(api_key=anthropic_key))
            if anthropic_key else None
        ),
    )
    # One date for every collector, even if the run crosses midnight.
    today = new_york_today(_utc_now())
    with Store(args.db) as store:
        result = run(store, tickers, today, sources, Path(args.digest_dir), args.lookback_days)

    print("\n".join(report_lines(result, sources)))
    if any(result.errors.values()):
        sys.exit(1)  # so a scheduler can tell the run had failures


if __name__ == "__main__":
    main()
