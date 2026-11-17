from datetime import date, datetime
from zoneinfo import ZoneInfo

from daily_digest_desk.collect import collect_signals
from daily_digest_desk.models import Reading
from daily_digest_desk.signals import (
    BENCHMARK, MACRO_RULES, BigMove, big_move_event, stock_rules,
)

NEW_YORK = ZoneInfo("America/New_York")

# Test rules, built here instead of taken from MACRO_RULES, so tuning a
# threshold in the real table doesn't break these tests.
TEN_YEAR = BigMove("fred:DGS10", "10-year Treasury yield", 0.10, change="diff", unit="%")
NVDA = BigMove("finnhub:NVDA", "NVDA", 5.0, change="pct", ticker="NVDA")
VIX = BigMove("fred:VIXCLS", "VIX", 3.0, change="diff")
DFF = BigMove("fred:DFF", "Fed funds rate", 0.20, change="diff", unit="%")
SPY = BigMove("finnhub:SPY", "S&P 500 (SPY)", 2.0, change="pct")  # no ticker: Macro & Market


def ten_year(day: date, value: float) -> Reading:
    return Reading(series="fred:DGS10", date=day, value=value)


# big_move_event: pure, so each test is just two readings and a rule.


def test_ten_year_big_up_is_an_event():
    previous = ten_year(date(2026, 11, 4), 4.90)
    latest = ten_year(date(2026, 11, 5), 5.15)
    event = big_move_event(TEN_YEAR, previous, latest)
    assert event is not None
    assert event.id == "signal:big_move:fred:DGS10:2026-11-05"
    assert event.source == "signal"
    assert event.kind == "big_move"
    assert event.ticker is None
    assert event.occurred_at == datetime(2026, 11, 5, 16, 0, tzinfo=NEW_YORK)
    assert event.title == "10-year Treasury yield up 0.25 points to 5.15%"
    assert event.url == "https://fred.stlouisfed.org/series/DGS10"
    assert event.fields == {
        "series": "fred:DGS10",
        "previous": 4.90,
        "latest": 5.15,
        "move": 0.25,
        "move_kind": "diff",
    }


def test_ten_year_big_drop_is_an_event():
    previous = ten_year(date(2026, 11, 4), 5.15)
    latest = ten_year(date(2026, 11, 5), 4.90)
    event = big_move_event(TEN_YEAR, previous, latest)
    assert event is not None
    assert event.id == "signal:big_move:fred:DGS10:2026-11-05"
    assert event.ticker is None
    assert event.occurred_at == datetime(2026, 11, 5, 16, 0, tzinfo=NEW_YORK)
    assert event.title == "10-year Treasury yield down 0.25 points to 4.90%"
    assert event.url == "https://fred.stlouisfed.org/series/DGS10"
    assert event.fields["move"] == -0.25


def test_ten_year_small_up_is_not_an_event():
    previous = ten_year(date(2026, 11, 4), 4.90)
    latest = ten_year(date(2026, 11, 5), 4.95)
    event = big_move_event(TEN_YEAR, previous, latest)
    assert event is None


def test_ten_year_up_exactly_at_threshold_is_an_event():
    # 5.00 - 4.90 is 0.09999999999999964 in floats; rounding first makes it 0.10.
    previous = ten_year(date(2026, 11, 4), 4.90)
    latest = ten_year(date(2026, 11, 5), 5.00)
    event = big_move_event(TEN_YEAR, previous, latest)
    assert event is not None
    assert event.id == "signal:big_move:fred:DGS10:2026-11-05"
    assert event.ticker is None
    assert event.occurred_at == datetime(2026, 11, 5, 16, 0, tzinfo=NEW_YORK)
    assert event.title == "10-year Treasury yield up 0.10 points to 5.00%"
    assert event.url == "https://fred.stlouisfed.org/series/DGS10"
    assert event.fields["move"] == 0.10


def test_stock_big_drop_is_an_event():
    previous = Reading(series="finnhub:NVDA", date=date(2026, 11, 4), value=199.80)
    latest = Reading(series="finnhub:NVDA", date=date(2026, 11, 5), value=187.40)
    event = big_move_event(NVDA, previous, latest)
    assert event is not None
    assert event.id == "signal:big_move:finnhub:NVDA:2026-11-05"
    assert event.ticker == "NVDA"
    assert event.occurred_at == datetime(2026, 11, 5, 16, 0, tzinfo=NEW_YORK)
    assert event.title == "NVDA down 6.21% to $187.40"
    assert event.url == "https://finance.yahoo.com/quote/NVDA"
    assert event.fields["move"] == -6.21
    assert event.fields["move_kind"] == "pct"


def test_stock_small_up_is_not_an_event():
    previous = Reading(series="finnhub:NVDA", date=date(2026, 11, 4), value=200.80)
    latest = Reading(series="finnhub:NVDA", date=date(2026, 11, 5), value=204.00)
    event = big_move_event(NVDA, previous, latest)
    assert event is None


def test_stock_up_exactly_at_threshold_is_an_event():
    # (126.21 - 120.20) / 120.20 * 100 is 4.999999999999993 in floats; rounding
    # first makes it 5.00, so it fires.
    previous = Reading(series="finnhub:NVDA", date=date(2026, 11, 4), value=120.20)
    latest = Reading(series="finnhub:NVDA", date=date(2026, 11, 5), value=126.21)
    event = big_move_event(NVDA, previous, latest)
    assert event is not None
    assert event.title == "NVDA up 5.00% to $126.21"
    assert event.fields["move"] == 5.0


def test_stock_previous_zero_is_not_an_event():
    previous = Reading(series="finnhub:NVDA", date=date(2026, 11, 4), value=0.00)
    latest = Reading(series="finnhub:NVDA", date=date(2026, 11, 5), value=100.00)
    event = big_move_event(NVDA, previous, latest)
    assert event is None


def test_vix_title_has_no_unit():
    previous = Reading(series="fred:VIXCLS", date=date(2026, 11, 4), value=18.00)
    latest = Reading(series="fred:VIXCLS", date=date(2026, 11, 5), value=22.00)
    event = big_move_event(VIX, previous, latest)
    assert event is not None
    assert event.id == "signal:big_move:fred:VIXCLS:2026-11-05"
    assert event.ticker is None
    assert event.occurred_at == datetime(2026, 11, 5, 16, 0, tzinfo=NEW_YORK)
    assert event.title == "VIX up 4.00 points to 22.00"
    assert event.url == "https://fred.stlouisfed.org/series/VIXCLS"
    assert event.fields["move"] == 4


def test_fed_funds_move_off_zero_is_an_event():
    previous = Reading(series="fred:DFF", date=date(2026, 11, 4), value=0.00)
    latest = Reading(series="fred:DFF", date=date(2026, 11, 5), value=0.25)
    event = big_move_event(DFF, previous, latest)
    assert event is not None
    assert event.id == "signal:big_move:fred:DFF:2026-11-05"
    assert event.ticker is None
    assert event.occurred_at == datetime(2026, 11, 5, 16, 0, tzinfo=NEW_YORK)
    assert event.title == "Fed funds rate up 0.25 points to 0.25%"
    assert event.url == "https://fred.stlouisfed.org/series/DFF"
    assert event.fields["move"] == 0.25


def test_move_over_missed_runs_says_since_when():
    # No runs Nov 9-12, so Nov 13's previous close is Nov 6: a week's move, not a day's.
    previous = Reading(series="finnhub:NVDA", date=date(2026, 11, 6), value=186.64)
    latest = Reading(series="finnhub:NVDA", date=date(2026, 11, 13), value=162.85)
    event = big_move_event(NVDA, previous, latest)
    assert event is not None
    assert event.title == "NVDA down 12.75% since Nov 6 to $162.85"


def test_long_weekend_gap_is_still_a_normal_day():
    # Friday to Tuesday is 4 days, the longest normal gap (a holiday Monday): no "since".
    previous = ten_year(date(2026, 11, 6), 4.90)
    latest = ten_year(date(2026, 11, 10), 5.15)
    event = big_move_event(TEN_YEAR, previous, latest)
    assert event is not None
    assert event.title == "10-year Treasury yield up 0.25 points to 5.15%"


def test_market_big_move_has_a_yahoo_link_and_no_ticker():
    previous = Reading(series="finnhub:SPY", date=date(2026, 11, 5), value=599.00)
    latest = Reading(series="finnhub:SPY", date=date(2026, 11, 6), value=585.12)
    event = big_move_event(SPY, previous, latest)
    assert event is not None
    assert event.title == "S&P 500 (SPY) down 2.32% to $585.12"
    assert event.url == "https://finance.yahoo.com/quote/SPY"
    assert event.ticker is None  # so the page puts it under Macro & Market
    assert event.fields == {
        "series": "finnhub:SPY", "previous": 599.00, "latest": 585.12,
        "move": -2.32, "move_kind": "pct",
    }


def test_market_small_move_is_not_an_event():
    previous = Reading(series="finnhub:SPY", date=date(2026, 11, 5), value=599.00)
    latest = Reading(series="finnhub:SPY", date=date(2026, 11, 6), value=590.00)  # -1.50%
    assert big_move_event(SPY, previous, latest) is None


def test_the_benchmark_rule_is_a_macro_rule():
    # The real table: one 2% rule for the benchmark, with no ticker.
    (rule,) = [r for r in MACRO_RULES if r.series == f"finnhub:{BENCHMARK}"]
    assert BENCHMARK == "SPY"
    assert (rule.threshold, rule.change, rule.ticker) == (2.0, "pct", None)


def test_stock_rules_one_pct_rule_per_ticker():
    assert stock_rules(["AAPL", "NVDA"]) == [
        BigMove("finnhub:AAPL", "AAPL", 5.0, change="pct", ticker="AAPL"),
        BigMove("finnhub:NVDA", "NVDA", 5.0, change="pct", ticker="NVDA"),
    ]
    assert stock_rules([]) == []


# collect_signals: needs the store, so these take the in-memory `store` fixture.
# Already-stored readings go in with one add_readings call; this run's readings
# are the return value of a second one, the same as main() passes.

def test_big_move_reading_becomes_an_event(store):
    # Already in the store from an earlier run. Not new, so we don't keep the return value.
    store.add_readings([ten_year(date(2026, 11, 4), 4.90)])

    # This run's new reading.
    new = store.add_readings([ten_year(date(2026, 11, 5), 5.15)])

    events, errors = collect_signals(store, new, [TEN_YEAR], date(2026, 11, 6))

    assert [e.title for e in events] == ["10-year Treasury yield up 0.25 points to 5.15%"]
    assert errors == {}


def test_small_move_reading_is_not_an_event(store):
    store.add_readings([ten_year(date(2026, 11, 4), 4.90)])
    new = store.add_readings([ten_year(date(2026, 11, 5), 4.95)])

    events, errors = collect_signals(store, new, [TEN_YEAR], date(2026, 11, 6))

    assert events == []
    assert errors == {}


def test_previous_can_come_from_the_same_run(store):
    # Missed Thursday's and Friday's runs; Monday's run brings both. Friday's
    # previous is Thursday, which was only stored in this same run.
    store.add_readings([ten_year(date(2026, 11, 4), 4.90)])
    new = store.add_readings([ten_year(date(2026, 11, 5), 5.15), ten_year(date(2026, 11, 6), 5.40)])

    events, errors = collect_signals(store, new, [TEN_YEAR], date(2026, 11, 9))

    assert [e.title for e in events] == [
        "10-year Treasury yield up 0.25 points to 5.15%",
        "10-year Treasury yield up 0.25 points to 5.40%",
    ]
    assert events[1].fields["previous"] == 5.15
    assert errors == {}


def test_reading_older_than_a_week_is_not_checked(store):
    # First-run backfill: a big move three weeks before today isn't news.
    new = store.add_readings([
        ten_year(date(2026, 10, 19), 4.90),
        ten_year(date(2026, 10, 20), 5.20),
    ])

    events, errors = collect_signals(store, new, [TEN_YEAR], date(2026, 11, 16))

    assert events == []
    assert errors == {}


def test_reading_exactly_a_week_old_is_checked(store):
    # The cutoff is "older than 7 days": Nov 9 is exactly 7 days before Nov 16, so it counts.
    store.add_readings([ten_year(date(2026, 11, 6), 4.90)])
    new = store.add_readings([ten_year(date(2026, 11, 9), 5.15)])

    events, errors = collect_signals(store, new, [TEN_YEAR], date(2026, 11, 16))

    assert len(events) == 1
    assert errors == {}


def test_reading_with_no_rule_is_skipped(store):
    # A series kept only as history (here DGS2, with no rule yet) is not an error.
    two_year = [
        Reading(series="fred:DGS2", date=date(2026, 11, 4), value=3.50),
        Reading(series="fred:DGS2", date=date(2026, 11, 5), value=4.00),
    ]
    new = store.add_readings(two_year)

    events, errors = collect_signals(store, new, [TEN_YEAR], date(2026, 11, 6))

    assert events == []
    assert errors == {}


def test_first_reading_of_a_series_is_skipped(store):
    # A holding's first day: nothing to compare with.
    new = store.add_readings([Reading(series="finnhub:NVDA", date=date(2026, 11, 5), value=187.40)])

    events, errors = collect_signals(store, new, [NVDA], date(2026, 11, 6))

    assert events == []
    assert errors == {}


def test_previous_outside_the_window_is_ignored(store):
    # Oct 23 is 13 days before Nov 5, beyond the 10-day search, so there's no previous.
    store.add_readings([ten_year(date(2026, 10, 23), 4.90)])
    new = store.add_readings([ten_year(date(2026, 11, 5), 5.15)])

    events, errors = collect_signals(store, new, [TEN_YEAR], date(2026, 11, 6))

    assert events == []
    assert errors == {}


def test_rerun_gives_no_new_events(store):
    store.add_readings([ten_year(date(2026, 11, 4), 4.90)])
    new = store.add_readings([ten_year(date(2026, 11, 5), 5.15)])

    first, first_errors = collect_signals(store, new, [TEN_YEAR], date(2026, 11, 6))
    second, second_errors = collect_signals(store, new, [TEN_YEAR], date(2026, 11, 6))

    assert len(first) == 1
    assert second == []  # same event id, so the store says it's not new
    assert first_errors == {} and second_errors == {}


def test_failed_reading_is_an_error_and_others_proceed(store):
    # A macro rule whose series has no "source:" prefix breaks the FRED url
    # (split(":")[1] raises IndexError). It's a made-up bad rule, to force an error.
    broken = BigMove("BROKEN", "Broken series", 0.10, change="diff")
    store.add_readings([
        Reading(series="BROKEN", date=date(2026, 11, 4), value=1.00),
        ten_year(date(2026, 11, 4), 4.90),
    ])
    new = store.add_readings([
        Reading(series="BROKEN", date=date(2026, 11, 5), value=2.00),
        ten_year(date(2026, 11, 5), 5.15),
    ])

    events, errors = collect_signals(store, new, [broken, TEN_YEAR], date(2026, 11, 6))

    assert list(errors) == ["BROKEN 2026-11-05"]
    assert [e.title for e in events] == ["10-year Treasury yield up 0.25 points to 5.15%"]
