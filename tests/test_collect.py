from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from daily_digest_desk.collect import collect_edgar, write_digest
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
