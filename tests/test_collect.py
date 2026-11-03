from datetime import date

from daily_digest_desk.collect import collect_edgar
from conftest import make_event


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
