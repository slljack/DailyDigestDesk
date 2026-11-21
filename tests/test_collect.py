import functools
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from daily_digest_desk import collect
from daily_digest_desk.collect import (
    NOT_DUE, SUMMARY_CAP_DOLLARS, RunResult, Sources, SummaryRun, collect_edgar, collect_summaries,
    new_york_today, report_lines, run, run_is_due, write_digest,
)
from daily_digest_desk.ingestion.edgar_text import MAX_CHARS
from daily_digest_desk.models import Reading
from daily_digest_desk.output.digest import build_digest
from daily_digest_desk.reasoning.summarize import PROMPT_VERSION, Summary, SummaryError, SummaryResult
from daily_digest_desk.store import Store
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
        self.summaries = []  # the summaries dict of each call

    def __call__(self, events, now, *, summaries):  # by keyword, as build_digest needs
        self.calls.append(([e.id for e in events], now))
        self.summaries.append(summaries)
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



def page_summary(text):
    return Summary(summary=text, key_numbers=[])


def save_summary(store, event_id, text):
    store.add_summary(event_id, page_summary(text), model="claude-opus-5-5", prompt_version=1,
                      input_tokens=1_000, output_tokens=100)


def test_the_page_gets_the_summaries_of_its_events_only(store, monkeypatch, tmp_path):
    add_at(store, monkeypatch, MORNING - timedelta(hours=1), [make_event("edgar:yesterday")])
    save_summary(store, "edgar:yesterday", "On the morning page.")
    write_digest(store, MORNING, tmp_path, FakeBuilder())
    add_at(store, monkeypatch, MORNING + timedelta(hours=3), [make_event("edgar:new"), make_event("edgar:form4")])
    save_summary(store, "edgar:new", "New today.")
    build = FakeBuilder()

    write_digest(store, EVENING, tmp_path, build)

    # The evening page's events: edgar:new has a summary, edgar:form4 none. The
    # morning filing's summary isn't sent, since that filing isn't on this page.
    assert build.calls == [(["edgar:form4", "edgar:new"], EVENING)]
    assert build.summaries == [{"edgar:new": page_summary("New today.")}]


def test_the_real_page_builder_shows_a_summary(store, monkeypatch, tmp_path):
    # With build_digest itself, set up as main() does: tickers filled in ahead,
    # so summaries has to arrive by keyword, not as the third argument.
    add_at(store, monkeypatch, MORNING - timedelta(hours=1), [make_event("edgar:1")])
    save_summary(store, "edgar:1", "Revenue was $57.0 billion.")
    build = functools.partial(build_digest, tickers=["AAPL"])

    path = write_digest(store, MORNING, tmp_path, build)

    assert '<span class="ai-tag">AI summary</span> Revenue was $57.0 billion.' in path.read_text(encoding="utf-8")



# --- run_is_due and --if-due: run only when a slot hasn't been covered ----------

def test_run_is_due_before_the_first_page(store):
    assert run_is_due(store, MORNING)


def test_run_is_not_due_right_after_a_page_and_due_at_the_next_slot(store, tmp_path):
    write_digest(store, MORNING, tmp_path, FakeBuilder())  # the cursor, as a real run sets it
    assert not run_is_due(store, MORNING + timedelta(hours=1))
    assert not run_is_due(store, datetime(2026, 11, 17, 16, 59, tzinfo=NEW_YORK))
    assert run_is_due(store, datetime(2026, 11, 17, 17, 0, tzinfo=NEW_YORK))


def test_run_is_due_after_a_skipped_slot(store, tmp_path):
    # The Nov 19 case: the morning page, then the 5pm run never happened.
    write_digest(store, MORNING, tmp_path, FakeBuilder())
    assert run_is_due(store, datetime(2026, 11, 17, 18, 24, tzinfo=NEW_YORK))


class Reached(Exception):
    """Raised by the fake run(), to show main() got past the --if-due check."""


def run_main(monkeypatch, tmp_path, now, *args):
    """main() with a test database, a fixed clock, no .env, and run() replaced."""
    holdings = tmp_path / "holdings.toml"
    holdings.write_text('[[position]]\nticker = "AAPL"\n')
    monkeypatch.setattr(collect, "load_dotenv", lambda: None)
    for key in ("FRED_API_KEY", "FINNHUB_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(collect, "_utc_now", lambda: now)

    def fake_run(*a, **kw):
        raise Reached
    monkeypatch.setattr(collect, "run", fake_run)
    monkeypatch.setattr("sys.argv", [
        "desk-collect", "--db", str(tmp_path / "events.db"), "--holdings", str(holdings),
        "--digest-dir", str(tmp_path / "digests"), *args,
    ])
    collect.main()


def page_built_at(tmp_path, when):
    with Store(tmp_path / "events.db") as s:
        write_digest(s, when, tmp_path / "digests", FakeBuilder())


def test_if_due_stops_when_not_due(monkeypatch, tmp_path, capsys):
    page_built_at(tmp_path, MORNING)
    run_main(monkeypatch, tmp_path, MORNING + timedelta(hours=1), "--if-due")  # no Reached
    assert capsys.readouterr().out.startswith(NOT_DUE)


def test_if_due_runs_when_due(monkeypatch, tmp_path, capsys):
    page_built_at(tmp_path, MORNING)
    with pytest.raises(Reached):
        run_main(monkeypatch, tmp_path, EVENING, "--if-due")
    assert NOT_DUE not in capsys.readouterr().out


def test_without_if_due_it_always_runs(monkeypatch, tmp_path):
    page_built_at(tmp_path, MORNING)
    with pytest.raises(Reached):
        run_main(monkeypatch, tmp_path, MORNING + timedelta(hours=1))  # by hand: not due, runs anyway


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
        "# Summaries skipped: set ANTHROPIC_API_KEY in .env",
        f"# Digest page: {(tmp_path / 'page.html').resolve()}",
        "# ERROR signals fred:DGS10 2026-11-17: boom",
    ]


# --- summaries inside a run ----------------------------------------------------------

class FakeSummaries:
    """Stands in for collect_summaries with the client filled in. Records the time
    it was given and whether a page had already been written when it ran."""

    def __init__(self, digest_dir=None, summary_run=None, errors=None, fails=None):
        self.digest_dir, self.summary_run = digest_dir, summary_run or SummaryRun(["edgar:1"], 0, 0.12)
        self.errors, self.fails, self.calls = errors or {}, fails, []

    def __call__(self, store, now):
        page_written = bool(self.digest_dir and any(self.digest_dir.glob("*.html")))
        self.calls.append((now, page_written))
        if self.fails:
            raise self.fails
        return self.summary_run, self.errors


def test_run_summarizes_before_writing_the_page(store, tmp_path):
    summaries = FakeSummaries(tmp_path)
    sources = Sources(edgar=FakeFetcher({}), summaries=summaries)
    result = run(store, ["AAPL"], TODAY, sources, tmp_path, clock=lambda: AFTER_CLOSE)
    assert summaries.calls == [(AFTER_CLOSE, False)]  # this run's time; no page yet
    assert result.summaries == summaries.summary_run
    assert result.errors["summaries"] == {}
    assert result.digest_path is not None


def test_run_summary_errors_are_the_runs_errors(store, tmp_path):
    summaries = FakeSummaries(errors={"edgar:1": "failed its checks twice"})
    result = run(store, ["AAPL"], TODAY, Sources(edgar=FakeFetcher({}), summaries=summaries), tmp_path)
    assert result.errors["summaries"] == {"edgar:1": "failed its checks twice"}


def test_run_summaries_crashing_still_writes_the_page(store, tmp_path):
    summaries = FakeSummaries(fails=RuntimeError("database is locked"))
    result = run(store, ["AAPL"], TODAY, Sources(edgar=FakeFetcher({}), summaries=summaries), tmp_path)
    assert result.errors["summaries"] == {"run": "database is locked"}
    assert result.summaries is None
    assert result.digest_path is not None


def test_run_without_a_summary_source_skips_summaries(store, tmp_path):
    result = run(store, ["AAPL"], TODAY, Sources(edgar=FakeFetcher({})), tmp_path)
    assert "summaries" not in result.errors and result.summaries is None


def report_with(summary_run, errors=None):
    result = RunResult(
        new=[], fred_readings=[], quote_readings=[], earnings_events=[], signal_events=[],
        digest_path=None, errors=errors or {}, summaries=summary_run,
    )
    return report_lines(result, Sources(edgar=FakeFetcher({}), summaries=FakeSummaries()))


def test_report_counts_summaries_and_their_cost():
    lines = report_with(SummaryRun(["edgar:1", "edgar:2", "edgar:3"], 0, 0.364))
    assert "# 3 new summary(ies), about $0.36" in lines
    assert not any("cap reached" in line for line in lines)


def test_report_says_when_the_cap_left_filings_for_the_next_run():
    lines = report_with(SummaryRun(["edgar:1"], 2, 2.05))
    assert "# 1 new summary(ies), about $2.05" in lines
    assert "# Summary cap reached: 2 filing(s) left for the next run" in lines


def test_report_after_summaries_crashed_shows_only_the_error():
    lines = report_with(None, errors={"summaries": {"run": "database is locked"}})
    assert not any("summary(ies)" in line for line in lines)
    assert "# Summaries skipped: set ANTHROPIC_API_KEY in .env" not in lines  # it wasn't skipped
    assert "# ERROR summaries run: database is locked" in lines


# --- collect_summaries ---------------------------------------------------------------

SUMMARY_NOW = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)  # a week back is day=3
CLIENT = object()  # never used by the fakes; only passed through


def filing(id, day=9, kind="8-K"):
    return make_event(id, day=day, kind=kind, fields={"accession_number": id.removeprefix("edgar:")})


def result(text="Revenue was $57.0 billion.", model="claude-opus-5-5",
           input_tokens=20_000, output_tokens=1_000):  # $0.08 + $0.02 = $0.10
    return SummaryResult(Summary(summary=text, key_numbers=[]), model, input_tokens, output_tokens)


class FakeDocuments:
    """Stands in for fetch_filing_documents: the same 8-K text for every filing,
    or an error for the accession numbers in `failing`."""

    def __init__(self, documents=(("8-K", "Item 2.02 Results."),), failing=()):
        self.documents, self.failing, self.calls = list(documents), set(failing), []

    def __call__(self, accession, form):
        self.calls.append((accession, form))
        if accession in self.failing:
            raise ConnectionError("SEC timed out")
        return self.documents


class FakeSummarizer:
    """Stands in for summarize_checked: answers by event id, `result()` by default.
    A SummaryError in `answers` is raised instead of returned."""

    def __init__(self, answers=None):
        self.answers, self.calls = answers or {}, []

    def __call__(self, event, documents, client):
        self.calls.append((event.id, documents, client))
        answer = self.answers.get(event.id, result())
        if isinstance(answer, SummaryError):
            raise answer
        return answer


def summaries(store, cap=SUMMARY_CAP_DOLLARS, fetch=None, summarize=None):
    return collect_summaries(
        store, CLIENT, SUMMARY_NOW, cap=cap,
        fetch=fetch or FakeDocuments(), summarize=summarize or FakeSummarizer(),
    )


def test_collect_summaries_saves_a_summary_with_its_model_and_tokens(store):
    store.add([filing("edgar:1")])
    answer = result(model="claude-fallback-model", input_tokens=20_000, output_tokens=1_000)
    run_, errors = summaries(store, summarize=FakeSummarizer({"edgar:1": answer}))
    assert (run_.saved, run_.left, errors) == (["edgar:1"], 0, {})
    assert run_.dollars == pytest.approx(0.10)
    (row,) = store._conn.execute("SELECT * FROM summaries").fetchall()
    assert row[:2] == ("edgar:1", "Revenue was $57.0 billion.")
    assert row[3:7] == ("claude-fallback-model", PROMPT_VERSION, 20_000, 1_000)


def test_collect_summaries_fetches_by_accession_and_form_and_trims(store):
    store.add([filing("edgar:0001104659-26-129051", kind="424B5")])
    fetch = FakeDocuments([("424B5", "x" * (MAX_CHARS + 10))])
    summarize = FakeSummarizer()
    summaries(store, fetch=fetch, summarize=summarize)
    assert fetch.calls == [("0001104659-26-129051", "424B5")]
    ((event_id, documents, client),) = summarize.calls
    assert documents == {"424B5": "x" * MAX_CHARS}  # trimmed before the model sees it
    assert client is CLIENT


def test_collect_summaries_only_recent_filings_of_summary_kinds(store):
    store.add([
        filing("edgar:recent", day=9),
        filing("edgar:old", day=2),            # more than a week before SUMMARY_NOW
        filing("edgar:form4", day=9, kind="4"),
    ])
    summarize = FakeSummarizer()
    summaries(store, summarize=summarize)
    assert [c[0] for c in summarize.calls] == ["edgar:recent"]


def test_collect_summaries_second_run_has_nothing_to_do(store):
    store.add([filing("edgar:1")])
    summaries(store)
    summarize = FakeSummarizer()
    run_, _ = summaries(store, summarize=summarize)
    assert summarize.calls == [] and run_.saved == [] and run_.dollars == 0


def test_collect_summaries_failed_summary_is_an_error_its_cost_counts_and_no_row(store):
    store.add([filing("edgar:bad", day=9), filing("edgar:good", day=8)])
    failed = SummaryError("Summary for edgar:bad failed its checks twice: ...", 40_000, 2_000)
    run_, errors = summaries(store, summarize=FakeSummarizer({"edgar:bad": failed}))
    assert errors == {"edgar:bad": str(failed)}
    assert run_.saved == ["edgar:good"]  # the next filing still gets its turn
    assert run_.dollars == pytest.approx(0.20 + 0.10)
    # No row, so a later run tries it again.
    assert [e.id for e in store.events_to_summarize(datetime(2026, 10, 1, tzinfo=timezone.utc), ["8-K"])] == ["edgar:bad"]


def test_collect_summaries_fetch_error_costs_nothing_and_others_proceed(store):
    store.add([filing("edgar:down", day=9), filing("edgar:fine", day=8)])
    summarize = FakeSummarizer()
    run_, errors = summaries(store, fetch=FakeDocuments(failing={"down"}), summarize=summarize)
    assert errors == {"edgar:down": "SEC timed out"}
    assert [c[0] for c in summarize.calls] == ["edgar:fine"]
    assert run_.dollars == pytest.approx(0.10)


def test_collect_summaries_filing_with_no_text_is_not_sent_to_the_model(store):
    store.add([filing("edgar:pictures")])
    summarize = FakeSummarizer()
    run_, errors = summaries(store, fetch=FakeDocuments([("8-K", ""), ("EX-99.1", "")]),
                             summarize=summarize)
    assert summarize.calls == []
    assert errors == {"edgar:pictures": "no text in the filing to summarize"}
    assert run_.dollars == 0


def test_collect_summaries_stops_at_the_cap_newest_first_going_over_by_one(store):
    store.add([filing(f"edgar:{day}", day=day) for day in (5, 6, 7, 8, 9)])
    summarize = FakeSummarizer()
    # Each costs $0.10. Under $0.25 a new one starts: at $0, $0.10, $0.20. Then $0.30: stop.
    run_, errors = summaries(store, cap=0.25, summarize=summarize)
    assert run_.saved == ["edgar:9", "edgar:8", "edgar:7"]
    assert run_.left == 2
    assert run_.dollars == pytest.approx(0.30)
    assert errors == {}


def test_collect_summaries_spending_exactly_the_cap_stops(store):
    store.add([filing(f"edgar:{day}", day=day) for day in (7, 8, 9)])
    run_, _ = summaries(store, cap=0.20)
    assert (len(run_.saved), run_.left) == (2, 1)


def test_collect_summaries_failed_calls_count_toward_the_cap(store):
    store.add([filing("edgar:bad", day=9), filing("edgar:next", day=8)])
    failed = SummaryError("Model refused to summarize edgar:bad", 60_000, 3_000)  # $0.30
    run_, errors = summaries(store, cap=0.25, summarize=FakeSummarizer({"edgar:bad": failed}))
    assert (run_.saved, run_.left) == ([], 1)
    assert list(errors) == ["edgar:bad"]


def test_collect_summaries_nothing_to_do(store):
    run_, errors = summaries(store)
    assert (run_.saved, run_.left, run_.dollars, errors) == ([], 0, 0, {})


def test_collect_summaries_filing_with_text_only_in_an_exhibit_is_still_summarized(store):
    # A cover page with no text, but a press release: there's something to read.
    store.add([filing("edgar:1")])
    summarize = FakeSummarizer()
    summaries(store, fetch=FakeDocuments([("8-K", ""), ("EX-99.1", "Revenue was $57.0 billion.")]),
              summarize=summarize)
    assert [c[0] for c in summarize.calls] == ["edgar:1"]
