"""Finnhub stock quotes -> one closing-price Reading per ticker per day.

`/quote` only returns the latest price, with no history. So a run during market
hours stores nothing (the price is still moving); any other run stores the
latest close. Repeat runs are harmless: the store dedupes on (series, date).

Fetching (`fetch_quote`) is separate from mapping (`quote_to_reading`) so the
mapping can be unit-tested with a fake quote and no network.

API docs: https://finnhub.io/docs/api/quote
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, time
from zoneinfo import ZoneInfo

from daily_digest_desk.models import Reading

_NEW_YORK = ZoneInfo("America/New_York")
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)
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
