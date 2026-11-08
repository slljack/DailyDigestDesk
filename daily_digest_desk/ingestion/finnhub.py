"""Finnhub: daily closing prices (Readings) and earnings reports (Events).

Quotes: `/quote` only returns the latest price, with no history. So a run during
market hours stores nothing (the price is still moving); any other run stores the
latest close. Repeat runs are harmless: the store dedupes on (series, date).

Earnings: `/calendar/earnings` lists a ticker's recent and upcoming reports. A
report becomes a heads-up Event when it's a few days away, and a result Event
once Finnhub fills in the actual EPS. Event ids leave out the report date,
because companies sometimes move it, so repeat runs are harmless here too.

Fetching is separate from mapping (`quote_to_reading`, `earnings_to_event`) so
the mappers can be unit-tested with fake responses and no network.

API docs: https://finnhub.io/docs/api/quote
          https://finnhub.io/docs/api/earnings-calendar
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from daily_digest_desk.models import Event, Reading

_NEW_YORK = ZoneInfo("America/New_York")
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)
_HEADS_UP_DAYS = 7
_LOOKBACK_DAYS = 7   # how far back to keep checking for late actuals
# Finnhub only says before open / after close; typical times, for sorting
_REPORT_TIME = {"bmo": time(8, 0), "amc": time(16, 0)}
_REPORT_HOUR_WORDS = {
    "bmo": "before market open",
    "amc": "after market close",
    "dmh": "during market hours",
}
_API_BASE = "https://finnhub.io/api/v1"


def fetch_quote(ticker: str, api_key: str) -> dict:
    """Finnhub's latest quote for one ticker, e.g. {"c": 181.6, "t": 1793912400, ...}.

    Raises ValueError for a ticker Finnhub doesn't know (it answers with all zeros).
    """
    params = {
        "symbol": ticker,
        "token": api_key,
    }

    query = urllib.parse.urlencode(params)
    # Errors raised here (HTTPError, URLError) don't include the URL, so the key isn't leaked.
    with urllib.request.urlopen(f"{_API_BASE}/quote?{query}", timeout=30) as resp:
        quote = json.load(resp)
        if quote["t"] == 0:
            raise ValueError(f"Finnhub has no quote for {ticker!r} (unknown ticker?)")
        return quote


def fetch_earnings(ticker: str, today: date, api_key: str) -> list[dict]:
    """Finnhub's earnings-calendar rows for one ticker, from a week ago to a week ahead.

    An unknown ticker gives an empty list, not an error.
    """
    params = {
        "symbol": ticker,
        "token": api_key,
        "from": (today - timedelta(days=_LOOKBACK_DAYS)).isoformat(),
        "to": (today + timedelta(days=_HEADS_UP_DAYS)).isoformat(),
    }

    query = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{_API_BASE}/calendar/earnings?{query}", timeout=30) as resp:
        earnings = json.load(resp)
        return earnings["earningsCalendar"]


def quote_to_reading(ticker: str, quote: dict, now: datetime) -> Reading | None:
    """The latest close as a Reading, or None while the market is open.

    `now` is when the quote was fetched; any time zone works. The reading's date
    is the New York day of the quote's timestamp `t`, not the day it was fetched.
    """
    now_ny = now.astimezone(_NEW_YORK)
    is_weekday = now_ny.weekday() < 5

    if is_weekday and _MARKET_OPEN <= now_ny.time() < _MARKET_CLOSE:
        return None  # "c" is a live price, not the close yet

    day = datetime.fromtimestamp(quote["t"], _NEW_YORK).date()

    return Reading(series=f"finnhub:{ticker.upper()}", date=day, value=float(quote["c"]))


def earnings_to_event(row: dict, today: date) -> Event | None:
    """One earnings-calendar row as an Event, or None if there's nothing to say yet.

    - Actual EPS filled in: a result Event (beat, missed or matched the estimate,
      compared in cents), dated when the company reported.
    - No actual yet, report within `_HEADS_UP_DAYS`: a heads-up Event, dated
      `today` so it appears in today's digest rather than on report day.
    - No actual yet, report date passed: None. Finnhub hasn't updated yet, and a
      later run will retry.
    - No actual yet, report further away: None. Too early for a heads-up.
    """
    report_day = date.fromisoformat(row["date"])
    report_time = _REPORT_TIME.get(row["hour"], time.min)   # time.min = 00:00
    report_id = f"finnhub:earnings:{row['symbol']}:{row['year']}Q{row['quarter']}"
    # The company's filing list, not the exact 8-K: Finnhub has no page per report.
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={row['symbol']}"
    if row["epsActual"] is not None:
        actual = round(row["epsActual"], 2)
        if row["epsEstimate"] is None:
            title = f"{row['symbol']} reported EPS {actual:.2f} (no analyst estimate)"
        else:
            estimate = round(row["epsEstimate"], 2)   # compare in cents, as the title shows
            if actual > estimate:
                verdict = "beat"
            elif actual < estimate:
                verdict = "missed"
            else:
                verdict = "matched"
            title = f"{row['symbol']} {verdict} EPS estimate: {actual:.2f} vs {estimate:.2f} expected"
        return Event(
            id=f"{report_id}:result",
            source="finnhub",
            kind="earnings_result",
            ticker=row["symbol"],
            occurred_at=datetime.combine(report_day, report_time, tzinfo=_NEW_YORK),
            title=title,
            url=url,
            fields={
                "eps_actual": row["epsActual"],
                "eps_estimate": row["epsEstimate"],
                "revenue_actual": row["revenueActual"],
                "revenue_estimate": row["revenueEstimate"],
                "fiscal_year": row["year"],
                "fiscal_quarter": row["quarter"],
            },
        )

    if today > report_day:
        return None  # reported, but Finnhub hasn't filled in actuals yet; a later run retries
    if (report_day - today).days > _HEADS_UP_DAYS:
        return None  # too early for a heads-up
    when = _REPORT_HOUR_WORDS.get(row["hour"], "(time not announced)")
    if row["epsEstimate"] is None:
        title = f"{row['symbol']} is reporting earnings on {report_day:%b %d} {when} (no analyst estimate)"
    else:
        title = f"{row['symbol']} is reporting earnings on {report_day:%b %d} {when}, EPS {row['epsEstimate']:.2f} expected"

    return Event(
        id=f"{report_id}:headsup",
        source="finnhub",
        kind="earnings_headsup",
        ticker=row["symbol"],
        occurred_at=datetime.combine(today, time.min, tzinfo=_NEW_YORK),
        title=title,
        url=url,
        fields={
            "eps_estimate": row["epsEstimate"],
            "revenue_estimate": row["revenueEstimate"],
            "fiscal_year": row["year"],
            "fiscal_quarter": row["quarter"],
            "report_day": row["date"],
            "hour": row["hour"],
        },
    )
