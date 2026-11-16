"""Signal rules: turn stored Readings into Events when something changes.

Readings (daily yields, VIX, closing prices) are history, not news. A rule
compares readings and makes an Event only when the change is worth telling.

v1 has one rule, the big move: today's reading vs the previous one, with a
threshold per series. Thresholds are starting points, to be tuned after a few
weeks of digests. Rules are pure (no store, no network), so tests need no setup.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from daily_digest_desk.models import Event, Reading


_NEW_YORK = ZoneInfo("America/New_York")
_MARKET_CLOSE = time(16, 0)

# The longest normal gap between trading days: Friday to Tuesday over a holiday Monday.
# Anything longer means runs were missed (a stock's close is only stored by a run).
_NORMAL_GAP_DAYS = 4


@dataclass(frozen=True)
class BigMove:
    """When a day-to-day change in one series counts as big."""

    series: str          # "fred:DGS10", "finnhub:NVDA"
    name: str            # for the title: "10-year Treasury yield"
    threshold: float     # how big counts as big, in the unit `change` gives
    change: str          # "diff" (points) or "pct" (percent), like fred.Release
    unit: str = ""       # after a macro level in the title: "%" for rates, "" for VIX
    ticker: str | None = None  # stock rules only: Yahoo url and a $ level in the title


# Thresholds are roughly 3-5x a normal day's move (checked on Oct-Nov 2026 data),
# so each fires a few times a month, not every day.
MACRO_RULES = [
    BigMove("fred:DGS10", "10-year Treasury yield", 0.10, change="diff", unit="%"),
    BigMove("fred:DFF", "Fed funds rate", 0.20, change="diff", unit="%"),  # i.e. the Fed moved
    BigMove("fred:VIXCLS", "VIX", 3.0, change="diff"),
]

STOCK_BIG_MOVE_PCT = 5.0  # same for every holding in v1; a per-ticker override can come later


def stock_rules(tickers: Iterable[str]) -> list[BigMove]:
    """One big-move rule per holding, built from its ticker."""
    return [
        BigMove(f"finnhub:{ticker}", ticker, STOCK_BIG_MOVE_PCT, change="pct", ticker=ticker)
        for ticker in tickers
    ]


def big_move_event(rule: BigMove, previous: Reading, latest: Reading) -> Event | None:
    """An Event if `latest` moved at least `rule.threshold` from `previous`, else None.

    `previous` is the reading just before `latest` in the same series (the last
    trading day, so a Monday compares with the Friday before). Up and down moves
    both count. The event is dated on `latest`'s day, so rerunning gives the same id.

    If `previous` is more than a long weekend back, the move covers several days,
    and the title says so: "NVDA down 12.75% since Nov 6 to $162.85".
    """
    difference = latest.value - previous.value
    if rule.change == "diff":
        move = difference  # points
    elif previous.value == 0:
        return None  # no percent change from 0; a $0 close would be bad data anyway
    else:
        move = difference / previous.value * 100  # percent

    size = round(abs(move), 2)  # rounded before the check, as the title shows
    if size < rule.threshold:
        return None

    direction = "up" if move > 0 else "down"

    # Only FRED and Finnhub produce readings: stock rules have a ticker, macro rules
    # are FRED series. A new source of readings needs its own branch here.
    if rule.ticker is not None:
        url = f"https://finance.yahoo.com/quote/{rule.ticker}"  # price chart and news
        level = f"${latest.value:.2f}"
    else:
        series_id = rule.series.split(":")[1]  # "fred:DGS10" -> "DGS10"
        url = f"https://fred.stlouisfed.org/series/{series_id}"
        level = f"{latest.value:.2f}{rule.unit}"

    size_text = f"{size:.2f} points" if rule.change == "diff" else f"{size:.2f}%"
    if (latest.date - previous.date).days > _NORMAL_GAP_DAYS:
        # .day, not %d, so it's "Nov 6", not "Nov 06"
        size_text += f" since {previous.date:%b} {previous.date.day}"
    title = f"{rule.name} {direction} {size_text} to {level}"

    return Event(
        id=f"signal:big_move:{rule.series}:{latest.date}",
        source="signal",
        kind="big_move",
        ticker=rule.ticker,
        occurred_at=datetime.combine(latest.date, _MARKET_CLOSE, tzinfo=_NEW_YORK),
        title=title,
        url=url,
        fields={
            "series": rule.series,
            "previous": previous.value,
            "latest": latest.value,
            "move": round(move, 2),  # signed, in the unit move_kind says
            "move_kind": rule.change,
        },
    )
