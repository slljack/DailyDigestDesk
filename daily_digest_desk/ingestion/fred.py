"""FRED (St. Louis Fed) macro series -> Readings and Events.

Two kinds of series, handled differently:

- Daily series (10Y yield, Fed funds rate, VIX) change every day, so each value is
  stored as a Reading. Signal rules (not built yet) turn changes into Events.
- Monthly releases (CPI, jobs) happen once a month and are news by themselves,
  so each new release becomes one Event, with the change vs the prior month.

Fetching (`fetch_observations`) is separate from mapping
(`observations_to_readings`, `latest_release_event`) so the mapping can be
unit-tested with fake observations and no network.

API docs: https://fred.stlouisfed.org/docs/api/fred/series_observations.html
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from daily_digest_desk.models import Event, Reading

_API_URL = "https://api.stlouisfed.org/fred/series/observations"
_NEW_YORK = ZoneInfo("America/New_York")

# Start of FRED's real-time history (the date FRED's docs use). A real-time window
# from here to today covers every version of every value ever published.
_ALL_TIME = "1776-07-04"

# FRED's marker for "no value that day" (e.g. a market holiday).
MISSING = "."

# Daily series stored as Readings.
DAILY_SERIES: tuple[str, ...] = ("DFF", "DGS10", "VIXCLS")


@dataclass(frozen=True)
class Release:
    """A monthly series whose new value is an Event."""

    series_id: str
    name: str        # shown in the title, e.g. "CPI"
    change: str      # "pct": % change vs prior month; "diff": plain difference
    unit: str = ""   # appended to a "diff" change, e.g. "k jobs"


RELEASES: dict[str, Release] = {
    r.series_id: r
    for r in (
        Release("CPIAUCSL", "CPI", change="pct"),
        Release("PAYEMS", "Jobs report", change="diff", unit="k jobs"),  # values are thousands
    )
}


def fetch_observations(
    series_id: str, since: date, api_key: str, first_release: bool = False
) -> list[dict]:
    """Raw FRED observations for one series with date >= since, oldest first.

    Each is a dict like {"date": "2026-11-02", "value": "4.98", "realtime_start": ...}.
    Values are strings, and "." means missing.

    By default you get today's version of each value (the latest revision), and
    `realtime_start` is just today. It is NOT the release date.
    With first_release=True you get each value's first published version instead,
    and `realtime_start` is the day it was first released. Use that for release
    dates only: its values are the unrevised first versions.
    """
    params = {
        "series_id": series_id,
        "observation_start": since.isoformat(),
        "api_key": api_key,
        "file_type": "json",
    }
    if first_release:
        # output_type=4 is "initial release only". It needs a real-time window
        # that reaches back past the release; FRED rejects today-only (HTTP 400).
        params.update(output_type=4, realtime_start=_ALL_TIME)
    query = urllib.parse.urlencode(params)
    # Errors raised here (HTTPError, URLError) don't include the URL, so the key isn't leaked.
    with urllib.request.urlopen(f"{_API_URL}?{query}", timeout=30) as resp:
        return json.load(resp)["observations"]


def _present(observations: list[dict]) -> list[dict]:
    return [o for o in observations if o["value"] != MISSING]


def observations_to_readings(series_id: str, observations: list[dict]) -> list[Reading]:
    """One Reading per day that has a value. Missing days (".") are skipped, not errors."""
    return [
        Reading(series=f"fred:{series_id}", date=date.fromisoformat(o["date"]), value=float(o["value"]))
        for o in _present(observations)
    ]


def latest_release_event(
    release: Release,
    observations: list[dict],
    first_releases: list[dict],
    since: date,
) -> Event | None:
    """An Event for the newest month, if it was first released on or after `since`.

    Two lists from FRED, used for different things:
    - `observations` (normal request): today's values, used for the change.
    - `first_releases` (first_release=True): used only for the release date.
    Needs the prior month too, to compute the change. Returns None otherwise.
    """
    present = sorted(_present(observations), key=lambda o: o["date"])
    if len(present) < 2:
        return None
    prev, latest = present[-2], present[-1]

    first_shown = {o["date"]: o["realtime_start"] for o in first_releases}
    if latest["date"] not in first_shown:
        # The two answers can disagree for a moment right after FRED posts a month.
        # No event now; the next run will see it in both.
        return None
    released_on = date.fromisoformat(first_shown[latest["date"]])
    if released_on < since:
        return None

    value, previous = float(latest["value"]), float(prev["value"])
    period = date.fromisoformat(latest["date"])
    if release.change == "pct":
        change = (value - previous) / previous * 100
        headline = f"{change:+.1f}% vs prior month"
    else:
        change = value - previous
        headline = f"{change:+,.0f}{release.unit} vs prior month"

    return Event(
        id=f"fred:{release.series_id}:{period.isoformat()}",
        source="fred",
        kind=release.series_id,
        ticker=None,
        # FRED gives only the release day, not the time. Start of that day in New York.
        occurred_at=datetime.combine(released_on, time.min, tzinfo=_NEW_YORK),
        title=f"{release.name} ({period:%b %Y}): {headline}",
        url=f"https://fred.stlouisfed.org/series/{release.series_id}",
        fields={
            "series_id": release.series_id,
            "period": period.isoformat(),
            "value": value,
            "previous": previous,
            "change": change,
            "change_kind": release.change,
            "released_on": released_on.isoformat(),
        },
    )
