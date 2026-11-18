from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from daily_digest_desk.collect import (
    RunResult, Sources, collect_edgar, new_york_today, report_lines, run, write_digest,
)
from daily_digest_desk.models import Reading
from conftest import add_at, make_event


class FakeFetcher:
    """Records the windows requested; returns canned events per ticker."""

    def __init__(self, events_by_ticker, failing=()):
        self.events_by_ticker = events_by_ticker
        self.failing = set(failing)
        self.calls: list[tuple[str, date]] = []

    def __call__(self, tickers, since):
        (ticker,) = tickers
        self.calls.append((ticker, since))
        if ticker in self.failing:
            return [], {ticker: "boom"}
        return self.events_by_ticker.get(ticker, []), {}


def test_first_run_uses_lookback_then_cursor(store):
    fetch = FakeFetcher({"AAPL": [make_event("edgar:1")]})
    new, errors = collect_edgar(store, ["AAPL"], date(2026, 10, 31), lookback_days=7, fetch=fetch)
    assert [e.id for e in new] == ["edgar:1"] and errors == {}
    assert fetch.calls == [("AAPL", date(2026, 10, 24))]

    # Next day: re-fetch from cursor (inclusive); the overlapping event is not "new".
    fetch.events_by_ticker["AAPL"].append(make_event("edgar:2"))
    new, _ = collect_edgar(store, ["AAPL"], date(2026, 11, 1), fetch=fetch)
    assert [e.id for e in new] == ["edgar:2"]
    assert fetch.calls[-1] == ("AAPL", date(2026, 10, 31))


def test_failed_ticker_keeps_cursor_and_others_proceed(store):
    fetch = FakeFetcher({"NVDA": [make_event("edgar:n", ticker="NVDA")]}, failing={"AAPL"})
    new, errors = collect_edgar(store, ["AAPL", "NVDA"], date(2026, 10, 31), fetch=fetch)
    assert [e.ticker for e in new] == ["NVDA"]
    assert errors == {"AAPL": "boom"}
    assert store.get_cursor("edgar:AAPL") is None
    assert store.get_cursor("edgar:NVDA") == "2026-10-31"


def test_new_ticker_gets_its_own_lookback(store):
    store.set_cursor("edgar:AAPL", "2026-10-30")
    fetch = FakeFetcher({})
    collect_edgar(store, ["AAPL", "CLSK"], date(2026, 10, 31), lookback_days=7, fetch=fetch)
    assert fetch.calls == [("AAPL", date(2026, 10, 30)), ("CLSK", date(2026, 10, 24))]


# --- write_digest: the digest cursor and the page file ------------------------

NEW_YORK = ZoneInfo("America/New_York")
MORNING = datetime(2026, 11, 17, 8, 0, 12, tzinfo=NEW_YORK)  # an 8am run
EVENING = datetime(2026, 11, 17, 17, 0, 5, tzinfo=NEW_YORK)  # the 5pm run


class FakeBuilder:
    """Records what it was given; returns a page that names the event ids."""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def __call__(self, events, now):
        self.calls.append(([e.id for e in events], now))
        if self.fail:
            raise RuntimeError("boom")
        return "page: " + ", ".join(e.id for e in events)


def test_first_digest_covers_the_past_week(store, monkeypatch, tmp_path):
    add_at(store, monkeypatch, MORNING - timedelta(days=8), [make_event("edgar:old")])
    add_at(store, monkeypatch, MORNING - timedelta(days=6), [make_event("edgar:recent")])
    build = FakeBuilder()

    path = write_digest(store, MORNING, tmp_path, build)

    assert build.calls == [(["edgar:recent"], MORNING)]
    assert path == tmp_path / "2026-11-17-080012.html"  # New York time, with seconds
    assert path.read_text(encoding="utf-8") == "page: edgar:recent"
    # Saved in UTC, the same format as inserted_at: 8:00:12am New York is 13:00:12 UTC.
    assert store.get_cursor("digest:last") == "2026-11-17T13:00:12+00:00"


def test_next_digest_starts_at_the_cursor(store, monkeypatch, tmp_path):
    add_at(store, monkeypatch, MORNING - timedelta(hours=1), [make_event("edgar:1")])
    write_digest(store, MORNING, tmp_path, FakeBuilder())
    add_at(store, monkeypatch, MORNING + timedelta(hours=3), [make_event("edgar:2")])
    build = FakeBuilder()

    path = write_digest(store, EVENING, tmp_path, build)

    assert build.calls == [(["edgar:2"], EVENING)]
    assert path.name == "2026-11-17-170005.html"
    assert sorted(p.name for p in tmp_path.iterdir()) == [  # the morning page is kept
        "2026-11-17-080012.html", "2026-11-17-170005.html", "latest.html",
    ]
    # latest.html is now the evening page, for a browser bookmark.
    assert (tmp_path / "latest.html").read_text(encoding="utf-8") == "page: edgar:2"


def test_nothing_new_still_writes_a_page(store, tmp_path):
    path = write_digest(store, MORNING, tmp_path, FakeBuilder())
    assert path.read_text(encoding="utf-8") == "page: "
    assert store.get_cursor("digest:last") == "2026-11-17T13:00:12+00:00"


def test_creates_the_folder(store, tmp_path):
    out_dir = tmp_path / "data" / "digests"
    path = write_digest(store, MORNING, out_dir, FakeBuilder())
    assert path.parent == out_dir and path.exists()


def test_failed_build_keeps_the_cursor(store, monkeypatch, tmp_path):
    add_at(store, monkeypatch, MORNING - timedelta(hours=1), [make_event("edgar:1")])
    with pytest.raises(RuntimeError):
        write_digest(store, MORNING, tmp_path, FakeBuilder(fail=True))
    assert store.get_cursor("digest:last") is None
    assert list(tmp_path.iterdir()) == []

    # The next digest still shows the event.
    build = FakeBuilder()
    write_digest(store, EVENING, tmp_path, build)
    assert build.calls == [(["edgar:1"], EVENING)]


def test_failed_write_keeps_the_cursor(store, tmp_path):
    blocker = tmp_path / "digests"
    blocker.write_text("a file where the folder should be")
    with pytest.raises(FileExistsError):
        write_digest(store, MORNING, blocker, FakeBuilder())
    assert store.get_cursor("digest:last") is None


# --- run: one whole desk-collect run, with fake sources -----------------------------

AFTER_CLOSE = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)  # 5pm New York, Tuesday
TODAY = date(2026, 11, 17)


class FakeQuotes:
    """Answers every ticker with the same close, at Tuesday 4pm New York."""

    def __init__(self, close=100.0, closes=None):
        self.close, self.closes, self.calls = close, closes or {}, []

    def __call__(self, ticker):
        self.calls.append(ticker)
        t = int(datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc).timestamp())
        return {"c": self.closes.get(ticker, self.close), "t": t}


def test_new_york_today():
    # 11:30pm in New York is already the next day in UTC, and 9:30pm in Denver.
    assert new_york_today(datetime(2026, 11, 18, 4, 30, tzinfo=timezone.utc)) == date(2026, 11, 17)
    assert new_york_today(datetime(2026, 11, 18, 5, 30, tzinfo=timezone.utc)) == date(2026, 11, 18)


def test_run_collects_every_source_then_writes_the_page(store, tmp_path):
    sources = Sources(
        edgar=FakeFetcher({"AAPL": [make_event("edgar:1")]}),
        fred=lambda series_id, since, first_release=False: [],
        quote=FakeQuotes(),
        earnings=lambda ticker, today: [],
    )
    result = run(store, ["AAPL"], TODAY, sources, tmp_path, clock=lambda: AFTER_CLOSE)

    assert [e.id for e in result.new] == ["edgar:1"]
    assert sources.quote.calls == ["AAPL", "SPY"]  # the holdings, then the benchmark
    assert [r.series for r in result.quote_readings] == ["finnhub:AAPL", "finnhub:SPY"]
    assert result.digest_path == tmp_path / "2026-11-17-170000.html"
    assert "AAPL event edgar:1" in result.digest_path.read_text(encoding="utf-8")
    assert result.errors == {
        "edgar": {}, "fred": {}, "finnhub_quotes": {}, "finnhub_earnings": {},
        "signals": {}, "digest": {},
    }


def test_run_fetches_the_benchmark_once_when_it_is_also_held(store, tmp_path):
    sources = Sources(edgar=FakeFetcher({}), quote=FakeQuotes())
    run(store, ["SPY", "AAPL"], TODAY, sources, tmp_path, clock=lambda: AFTER_CLOSE)
    assert sources.quote.calls == ["SPY", "AAPL"]


def test_run_skips_sources_without_a_fetcher(store, tmp_path):
    result = run(store, ["AAPL"], TODAY, Sources(edgar=FakeFetcher({})), tmp_path)
    assert set(result.errors) == {"edgar", "signals", "digest"}  # no FRED or Finnhub keys
    assert result.quote_readings == [] and result.fred_readings == []
    assert result.digest_path is not None


def test_run_turns_this_runs_prices_into_big_moves(store, tmp_path):
    # Monday's SPY close is stored; Tuesday's is 2.5% lower: a Macro & Market event.
    store.add_readings([Reading(series="finnhub:SPY", date=date(2026, 11, 16), value=600.0)])
    sources = Sources(edgar=FakeFetcher({}), quote=FakeQuotes(closes={"SPY": 585.0}))
    result = run(store, ["AAPL"], TODAY, sources, tmp_path, clock=lambda: AFTER_CLOSE)
    assert [(e.title, e.ticker) for e in result.signal_events] == [
        ("S&P 500 (SPY) down 2.50% to $585.00", None),
    ]
    assert result.signal_events[0] in result.new


def test_run_page_failure_is_an_error_but_the_data_is_kept(store, tmp_path):
    blocker = tmp_path / "digests"
    blocker.write_text("a file where the folder should be")
    sources = Sources(edgar=FakeFetcher({"AAPL": [make_event("edgar:1")]}))
    result = run(store, ["AAPL"], TODAY, sources, blocker)
    assert result.digest_path is None
    assert set(result.errors["digest"]) == {"page"}
    assert store.get_cursor("digest:last") is None  # the next page shows it again
    assert [e.id for e in store.events_inserted_since(datetime(2000, 1, 1, tzinfo=timezone.utc))] == ["edgar:1"]


def test_next_runs_page_does_not_repeat_this_runs_events(store, tmp_path):
    # The page's cursor must be read after this run stored its events, not before:
    # with a stale time, this run's events would come back on the next page.
    sources = Sources(edgar=FakeFetcher({"AAPL": [make_event("edgar:1")]}))
    first = run(store, ["AAPL"], TODAY, sources, tmp_path / "1")
    second = run(store, ["AAPL"], TODAY, sources, tmp_path / "2")
    assert "AAPL event edgar:1" in first.digest_path.read_text(encoding="utf-8")
    assert "Nothing new since the last digest." in second.digest_path.read_text(encoding="utf-8")


def test_report_lists_events_counts_page_and_errors(tmp_path):
    result = RunResult(
        new=[make_event("edgar:1")], fred_readings=[], quote_readings=[], earnings_events=[],
        signal_events=[], digest_path=tmp_path / "page.html",
        errors={"edgar": {}, "signals": {"fred:DGS10 2026-11-17": "boom"}, "digest": {}},
    )
    lines = report_lines(result, Sources(edgar=FakeFetcher({})))
    assert lines == [
        "2026-10-01 20:00Z  AAPL event edgar:1\n    https://example.test",
        "# 1 new event(s)",
        "# FRED skipped: set FRED_API_KEY in .env",
        "# Finnhub skipped: set FINNHUB_API_KEY in .env",
        "# 0 new signal event(s), included above",
        f"# Digest page: {(tmp_path / 'page.html').resolve()}",
        "# ERROR signals fred:DGS10 2026-11-17: boom",
    ]
