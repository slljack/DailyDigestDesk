from datetime import date

import pytest
from pydantic import ValidationError

from daily_digest_desk.models import Reading
from daily_digest_desk.store import Store


def r(series: str = "fred:DGS10", day: int = 3, value: float = 4.98) -> Reading:
    return Reading(series=series, date=date(2026, 11, day), value=value)


def test_add_readings_returns_only_new(store):
    first = store.add_readings([r(day=3), r(day=4)])
    second = store.add_readings([r(day=4), r(day=5)])
    assert [x.date.day for x in first] == [3, 4]
    assert [x.date.day for x in second] == [5]


def test_same_series_and_date_stored_once_first_value_wins(store):
    store.add_readings([r(day=3, value=4.98)])
    assert store.add_readings([r(day=3, value=5.10)]) == []
    (got,) = store.readings_since("fred:DGS10", date(2026, 11, 1))
    assert got.value == 4.98


def test_same_date_different_series_both_kept(store):
    new = store.add_readings([r("fred:DGS10"), r("finnhub:NVDA", value=191.40)])
    assert len(new) == 2


def test_readings_since_filters_series_and_date_and_orders(store):
    store.add_readings([
        r(day=5, value=5.05),
        r(day=3, value=4.98),
        r(day=1, value=4.90),             # before `since`
        r("finnhub:NVDA", day=4, value=191.40),  # other series
    ])
    got = store.readings_since("fred:DGS10", date(2026, 11, 3))
    assert [(x.date, x.value) for x in got] == [
        (date(2026, 11, 3), 4.98),
        (date(2026, 11, 5), 5.05),
    ]


def test_readings_persist_to_disk(tmp_path):
    path = tmp_path / "events.db"
    with Store(path) as s:
        s.add_readings([r()])
    with Store(path) as s:
        assert s.add_readings([r()]) == []
        assert len(s.readings_since("fred:DGS10", date(2026, 11, 1))) == 1


def test_reading_rejects_malformed_date():
    with pytest.raises(ValidationError):
        Reading(series="fred:DGS10", date="2026-11-3", value=4.98)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "nan"])
def test_reading_rejects_nan_and_infinity(bad):
    with pytest.raises(ValidationError):
        Reading(series="fred:DGS10", date=date(2026, 11, 3), value=bad)
