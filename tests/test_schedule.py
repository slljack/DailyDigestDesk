from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from daily_digest_desk.schedule import is_due, latest_slot

NEW_YORK = ZoneInfo("America/New_York")


def ny(day, hour, minute=0, second=0, month=11):
    """A New York time in November 2026. Nov 16 is a Monday, Nov 20 a Friday."""
    return datetime(2026, month, day, hour, minute, second, tzinfo=NEW_YORK)


# --- latest_slot: the most recent 8am or 5pm, weekdays --------------------------

@pytest.mark.parametrize("now, slot", [
    (ny(17, 10), ny(17, 8)),        # Tuesday morning: today's 8am
    (ny(17, 16, 59), ny(17, 8)),    # Tuesday just before 5pm: still 8am
    (ny(17, 18), ny(17, 17)),       # Tuesday evening: today's 5pm
    (ny(17, 23, 59), ny(17, 17)),   # Tuesday just before midnight
    (ny(18, 7), ny(17, 17)),        # Wednesday before 8am: yesterday's 5pm
])
def test_latest_slot_on_weekdays(now, slot):
    assert latest_slot(now) == slot


def test_latest_slot_at_exactly_the_slot_is_that_slot():
    assert latest_slot(ny(17, 8)) == ny(17, 8)
    assert latest_slot(ny(17, 7, 59, 59)) == ny(16, 17)  # one second before: the day before


@pytest.mark.parametrize("now", [
    ny(20, 18),       # Friday evening
    ny(21, 12),       # Saturday
    ny(22, 23),       # Sunday night
    ny(23, 7, 59),    # Monday just before 8am
])
def test_latest_slot_over_the_weekend_is_friday_5pm(now):
    assert latest_slot(now) == ny(20, 17)


def test_latest_slot_monday_8am_ends_the_weekend():
    assert latest_slot(ny(23, 8)) == ny(23, 8)


def test_latest_slot_from_another_time_zone():
    # 6:30am in Denver (UTC-7) is 8:30am in New York: the 8am slot has passed.
    denver = timezone(timedelta(hours=-7))
    assert latest_slot(datetime(2026, 11, 17, 6, 30, tzinfo=denver)) == ny(17, 8)
    # 5:59am in Denver is 7:59am in New York: not yet.
    assert latest_slot(datetime(2026, 11, 17, 5, 59, tzinfo=denver)) == ny(16, 17)


def test_latest_slot_keeps_8am_new_york_across_daylight_saving_time():
    # Daylight saving time ended Sunday Nov 1, 2026: 8am moved from 12:00 to 13:00 UTC.
    utc = timezone.utc
    assert latest_slot(datetime(2026, 10, 30, 12, 30, tzinfo=utc)) == datetime(2026, 10, 30, 12, 0, tzinfo=utc)
    assert latest_slot(datetime(2026, 11, 2, 13, 30, tzinfo=utc)) == datetime(2026, 11, 2, 13, 0, tzinfo=utc)
    # 12:30 UTC on Monday Nov 2 is 7:30am New York: still Friday's 5pm (21:00 UTC then).
    assert latest_slot(datetime(2026, 11, 2, 12, 30, tzinfo=utc)) == datetime(2026, 10, 30, 21, 0, tzinfo=utc)


def test_latest_slot_needs_a_time_zone():
    with pytest.raises(ValueError):
        latest_slot(datetime(2026, 11, 17, 10, 0))


# --- is_due: no page since the most recent slot ------------------------------

def test_is_due_before_the_first_page():
    assert is_due(None, ny(17, 10))


def test_is_due_when_the_last_page_is_older_than_the_slot():
    # The Nov 19 case: the 3pm Denver run was skipped, so at 6pm New York the
    # last page is still that morning's.
    assert is_due(ny(19, 8, 7), ny(19, 18))


def test_not_due_when_a_page_was_built_after_the_slot():
    assert not is_due(ny(19, 17, 5), ny(19, 18))


def test_not_due_when_the_page_was_built_at_exactly_the_slot():
    assert not is_due(ny(19, 17), ny(19, 18))


def test_a_run_by_hand_covers_its_slot_but_not_the_next():
    by_hand = ny(17, 14)  # 2pm: after the 8am slot
    assert not is_due(by_hand, ny(17, 16, 59))
    assert is_due(by_hand, ny(17, 17))


def test_not_due_over_the_weekend_after_fridays_evening_page():
    assert not is_due(ny(20, 17, 3), ny(22, 12))
    assert is_due(ny(20, 17, 3), ny(23, 8, 1))  # Monday 8am is a new slot


def test_is_due_compares_times_not_text():
    # The cursor is stored in UTC; a New York `now` must still compare right.
    last_page = datetime(2026, 11, 19, 22, 5, tzinfo=timezone.utc)  # 5:05pm New York
    assert not is_due(last_page, ny(19, 18))
