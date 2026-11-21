"""When a desk-collect run is due: the 8am and 5pm New York slots, weekdays.

launchd can't be trusted to make up a slot the Mac slept through, so it starts
a check every few minutes instead, and a run happens only when one is due:
when no page has been built since the most recent slot.

Public surface:
    latest_slot(now)          -> datetime  (the most recent slot at or before now)
    is_due(last_page, now)    -> bool
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

_NEW_YORK = ZoneInfo("America/New_York")

# Before the market opens, and after it closes (4pm) with time for the day's
# filings and closing prices to come in.
SLOT_HOURS = (8, 17)


def latest_slot(now: datetime) -> datetime:
    """The most recent slot at or before `now`, in New York time.

    Weekdays only: on Saturday, Sunday or Monday before 8am it's Friday 5pm.
    Market holidays still count; a run on one just finds little that's new.
    """
    if now.tzinfo is None:
        raise ValueError("now must have a time zone")
    day = now.astimezone(_NEW_YORK).date()
    while True:  # at most three days back, from Monday morning to Friday
        if day.weekday() < 5:  # Monday is 0, Friday is 4
            for hour in reversed(SLOT_HOURS):  # the later slot first
                # combine() with the zone gives 8am wall-clock time in New York,
                # so daylight saving time moves the slot's UTC time, not its hour.
                slot = datetime.combine(day, time(hour), tzinfo=_NEW_YORK)
                if slot <= now:
                    return slot
        day -= timedelta(days=1)


def is_due(last_page: datetime | None, now: datetime) -> bool:
    """True when no page has been built since the most recent slot.

    `last_page` is when the last page was built (the digest cursor), or None
    before the first one. A page built at exactly the slot counts as that slot's.
    """
    return last_page is None or last_page < latest_slot(now)
