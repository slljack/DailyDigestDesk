from datetime import date, datetime, timezone

import pytest

from daily_digest_desk.collect import collect_fred
from daily_digest_desk.ingestion import fred

# Fake FRED rows are shaped like real responses (checked live on 2026-11-04):
# - a normal request stamps every row with the day you asked (today), and
# - a first_release=True request stamps each row with its first release day.
TODAY = "2026-11-04"


def obs(day: str, value: str) -> dict:
    """A row from a normal request: today's version, realtime_start = today."""
    return {"date": day, "value": value, "realtime_start": TODAY, "realtime_end": TODAY}


def first(day: str, value: str, released: str) -> dict:
    """A row from a first_release=True request: realtime_start = first release day."""
    return {"date": day, "value": value, "realtime_start": released, "realtime_end": "9999-12-31"}


# --- mapping: daily series -> readings -------------------------------------------------

def test_readings_parse_values_and_skip_missing_days():
    got = fred.observations_to_readings("DGS10", [
        obs("2026-11-02", "4.98"),
        obs("2026-11-03", "."),          # holiday: no value
        obs("2026-11-04", "5.02"),
    ])
    assert [(r.series, r.date, r.value) for r in got] == [
        ("fred:DGS10", date(2026, 11, 2), 4.98),
        ("fred:DGS10", date(2026, 11, 4), 5.02),
    ]


# --- mapping: monthly release -> one event ---------------------------------------------

CPI = fred.RELEASES["CPIAUCSL"]
JOBS = fred.RELEASES["PAYEMS"]

# Real CPI numbers. CPI is rarely revised, so both requests have the same values.
CPI_NOW = [obs("2026-08-01", "334.131"), obs("2026-09-01", "336.298")]
CPI_FIRST = [
    first("2026-08-01", "334.131", released="2026-09-11"),
    first("2026-09-01", "336.298", released="2026-10-14"),
]

# Real jobs numbers. August was revised on Oct 2, so the two requests differ.
JOBS_NOW = [obs("2026-08-01", "159015"), obs("2026-09-01", "159044")]
JOBS_FIRST = [
    first("2026-08-01", "159075", released="2026-09-04"),   # August's first, unrevised value
    first("2026-09-01", "159044", released="2026-10-02"),
]


def test_cpi_release_reports_percent_change():
    event = fred.latest_release_event(CPI, CPI_NOW, CPI_FIRST, since=date(2026, 10, 1))
    assert event.id == "fred:CPIAUCSL:2026-09-01"
    assert event.title == "CPI (Sep 2026): +0.6% vs prior month"
    assert event.ticker is None and event.kind == "CPIAUCSL"


def test_release_date_comes_from_first_release_not_todays_request():
    # The bug the live run found: the normal request says "today" for every row.
    event = fred.latest_release_event(CPI, CPI_NOW, CPI_FIRST, since=date(2026, 10, 1))
    assert event.fields["released_on"] == "2026-10-14"
    # Midnight in New York is 04:00 UTC in October (daylight time).
    assert event.occurred_at == datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)


def test_change_uses_revised_values_not_first_release_values():
    # Current: 159044 - 159015 = +29k (matches the headline).
    # Using August's first value (159075) would give -31k.
    event = fred.latest_release_event(JOBS, JOBS_NOW, JOBS_FIRST, since=date(2026, 10, 1))
    assert event.title == "Jobs report (Sep 2026): +29k jobs vs prior month"
    assert event.fields["released_on"] == "2026-10-02"


def test_release_before_since_is_not_news():
    assert fred.latest_release_event(CPI, CPI_NOW, CPI_FIRST, since=date(2026, 10, 15)) is None


def test_no_event_if_newest_month_missing_from_first_releases():
    # Right after FRED posts a month, the two requests can disagree for a moment.
    assert fred.latest_release_event(CPI, CPI_NOW, CPI_FIRST[:1], since=date(2026, 10, 1)) is None


def test_release_needs_a_prior_month():
    now = [obs("2026-08-01", "."), obs("2026-09-01", "336.298")]
    assert fred.latest_release_event(CPI, now, CPI_FIRST, since=date(2026, 10, 1)) is None


# --- collector -----------------------------------------------------------------------

class FakeFred:
    """Records the requests made; returns canned rows per (series, first_release)."""

    def __init__(self, now=None, firsts=None, failing=()):
        self.now = now or {}
        self.firsts = firsts or {}
        self.failing = set(failing)
        self.calls: list[tuple[str, date, bool]] = []

    def __call__(self, series_id, since, first_release=False):
        self.calls.append((series_id, since, first_release))
        if series_id in self.failing:
            raise OSError("boom")
        return (self.firsts if first_release else self.now).get(series_id, [])


def cpi_and_10y() -> FakeFred:
    return FakeFred(
        now={"DGS10": [obs("2026-11-02", "4.98")], "CPIAUCSL": CPI_NOW},
        firsts={"CPIAUCSL": CPI_FIRST},
    )


def test_collect_stores_readings_and_release_events(store):
    fetch = cpi_and_10y()
    readings, events, errors = collect_fred(store, date(2026, 11, 3), fetch, lookback_days=30)
    assert [r.series for r in readings] == ["fred:DGS10"]
    assert [e.id for e in events] == ["fred:CPIAUCSL:2026-09-01"]
    assert errors == {}


def test_collect_makes_one_request_per_daily_series_and_two_per_release(store):
    fetch = cpi_and_10y()
    collect_fred(store, date(2026, 11, 3), fetch, lookback_days=30)
    # First run: daily series look back 30 days (Oct 4).
    assert ("DGS10", date(2026, 10, 4), False) in fetch.calls
    assert ("DGS10", date(2026, 10, 4), True) not in fetch.calls
    # Releases look 100 more days back, for the prior month; once normal, once first-release.
    assert ("CPIAUCSL", date(2026, 6, 26), False) in fetch.calls
    assert ("CPIAUCSL", date(2026, 6, 26), True) in fetch.calls


def test_rerun_returns_nothing_new(store):
    fetch = cpi_and_10y()
    collect_fred(store, date(2026, 11, 3), fetch)
    readings, events, _ = collect_fred(store, date(2026, 11, 3), fetch)
    assert readings == [] and events == []


def test_daily_cursor_is_newest_value_not_today(store):
    # FRED posts daily values late: on Nov 3, the newest value may be Nov 1.
    fetch = FakeFred(now={"DGS10": [obs("2026-10-31", "4.95"), obs("2026-11-01", "4.98")]})
    collect_fred(store, date(2026, 11, 3), fetch)
    assert store.get_cursor("fred:DGS10") == "2026-11-01"


def test_failed_series_keeps_cursor_and_others_proceed(store):
    fetch = FakeFred(now={"DFF": [obs("2026-11-02", "4.33")]}, failing={"DGS10"})
    readings, _, errors = collect_fred(store, date(2026, 11, 3), fetch)
    assert [r.series for r in readings] == ["fred:DFF"]
    assert errors == {"DGS10": "boom"}
    assert store.get_cursor("fred:DGS10") is None


# --- fetch (network replaced) ----------------------------------------------------------

@pytest.fixture
def captured_url(monkeypatch):
    """Replace the network: record the URL requested and return one fake row."""
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def read(self):
            return b'{"observations": [{"date": "2026-11-02", "value": "4.98"}]}'

    def fake_urlopen(url, timeout):
        seen["url"] = url
        return FakeResponse()

    monkeypatch.setattr(fred.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_fetch_sends_series_start_and_key(captured_url):
    got = fred.fetch_observations("DGS10", date(2026, 10, 1), api_key="test-key")
    assert got == [{"date": "2026-11-02", "value": "4.98"}]
    url = captured_url["url"]
    assert "series_id=DGS10" in url
    assert "observation_start=2026-10-01" in url
    assert "api_key=test-key" in url
    assert "file_type=json" in url
    # A normal request asks for today's version: no real-time settings.
    assert "output_type" not in url and "realtime_start" not in url


def test_fetch_first_release_adds_real_time_settings(captured_url):
    fred.fetch_observations("CPIAUCSL", date(2026, 7, 1), api_key="test-key", first_release=True)
    url = captured_url["url"]
    assert "output_type=4" in url
    assert "realtime_start=1776-07-04" in url
    assert "observation_start=2026-07-01" in url
