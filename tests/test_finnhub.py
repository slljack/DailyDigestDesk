import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from daily_digest_desk.collect import collect_finnhub
from daily_digest_desk.ingestion import finnhub
from daily_digest_desk.ingestion.finnhub import fetch_quote, quote_to_reading

NEW_YORK = ZoneInfo("America/New_York")

# Real NVDA quote from Finnhub, fetched 2026-11-05 before the open.
# t = 2026-11-04 16:00 New York, so this is Wednesday's close.
QUOTE = {"c": 200.13, "d": -6.56, "dp": -3.1738, "h": 206.79, "l": 199.68,
         "o": 203.5, "pc": 206.69, "t": 1793826000}


def test_quote_before_open_is_previous_days_close():
    now = datetime(2026, 11, 5, 8, 0, tzinfo=NEW_YORK)
    reading = quote_to_reading("NVDA", QUOTE, now)
    assert reading.series == "finnhub:NVDA"
    assert reading.date == date(2026, 11, 4)
    assert reading.value == 200.13


def test_quote_market_open_return_none():
    now = datetime(2026, 11, 4, 11, 0, tzinfo=NEW_YORK)
    reading = quote_to_reading("NVDA", QUOTE, now)
    assert reading is None


def test_quote_saturday_is_fridays_close():
    friday = {**QUOTE, "t": int(datetime(2026, 11, 6, 16, 0, tzinfo=NEW_YORK).timestamp())}
    now = datetime(2026, 11, 7, 11, 0, tzinfo=NEW_YORK)
    reading = quote_to_reading("NVDA", friday, now)
    assert reading.series == "finnhub:NVDA"
    assert reading.date == date(2026, 11, 6)
    assert reading.value == 200.13


def test_quote_after_close_is_todays_close():
    now = datetime(2026, 11, 4, 17, 0, tzinfo=NEW_YORK)
    reading = quote_to_reading("NVDA", QUOTE, now)
    assert reading.series == "finnhub:NVDA"
    assert reading.date == date(2026, 11, 4)
    assert reading.value == 200.13


def test_now_in_utc_is_converted_to_new_york():
    # 20:00 UTC is 3 pm in New York: market open. Read as 20:00 it would look closed.
    now = datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc)
    assert quote_to_reading("NVDA", QUOTE, now) is None


# --- fetch (network replaced) ----------------------------------------------------------

@pytest.fixture
def fake_finnhub(monkeypatch):
    """Replace the network: record the URL requested and answer with fake["body"]."""
    fake = {"body": QUOTE}  # a test can swap in another body before calling

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def read(self):
            return json.dumps(fake["body"]).encode()

    def fake_urlopen(url, timeout):
        fake["url"] = url
        return FakeResponse()

    monkeypatch.setattr(finnhub.urllib.request, "urlopen", fake_urlopen)
    return fake


def test_fetch_quote_sends_symbol_and_token(fake_finnhub):
    got = fetch_quote("NVDA", api_key="test-key")
    assert got == QUOTE
    url = fake_finnhub["url"]
    assert "/quote?" in url
    assert "symbol=NVDA" in url
    assert "token=test-key" in url


def test_fetch_quote_unknown_ticker_raises(fake_finnhub):
    fake_finnhub["body"] = {"c": 0, "t": 0}      # what Finnhub sends for an unknown ticker
    with pytest.raises(ValueError):
        fetch_quote("NVDAA", api_key="test-key")


# --- collector -----------------------------------------------------------------------

class FakeFinnhub:
    """Stands in for fetch_quote with the key filled in: QUOTE for every ticker, or raises."""

    def __init__(self, failing=()):
        self.failing = set(failing)
        self.calls: list[str] = []

    def __call__(self, ticker):
        self.calls.append(ticker)
        if ticker in self.failing:
            raise OSError("boom")
        return QUOTE


def test_tickers_become_stored_readings_after_close(store):
    fetch = FakeFinnhub()
    now = datetime(2026, 11, 4, 17, 0, tzinfo=NEW_YORK)
    readings, errors = collect_finnhub(store, ["NVDA"], now, fetch)
    assert [r.series for r in readings] == ["finnhub:NVDA"]
    assert errors == {}


def test_one_failing_ticker_does_not_stop_others(store):
    fetch = FakeFinnhub(failing={"NVDA"})
    now = datetime(2026, 11, 4, 17, 0, tzinfo=NEW_YORK)
    readings, errors = collect_finnhub(store, ["NVDA", "AAPL"], now, fetch)
    assert [r.series for r in readings] == ["finnhub:AAPL"]
    assert errors == {"NVDA": "boom"}


def test_market_hours_store_nothing(store):
    fetch = FakeFinnhub()
    now = datetime(2026, 11, 4, 15, 0, tzinfo=NEW_YORK)
    readings, errors = collect_finnhub(store, ["NVDA", "AAPL"], now, fetch)
    assert readings == []
    assert errors == {}
