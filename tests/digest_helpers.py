"""Helpers shared by the digest page tests (test_digest, test_explainers, test_glossary)."""

import re
from datetime import datetime, timedelta, timezone

from daily_digest_desk.models import Event

NOW = datetime(2026, 11, 17, 13, 0, 12, tzinfo=timezone.utc)  # Tue 8:00am New York


def event(id, kind="8-K", ticker="AAPL", hours_ago=1, title=None, url="https://sec.test/x"):
    return Event(
        id=id, source="test", kind=kind, ticker=ticker,
        occurred_at=NOW - timedelta(hours=hours_ago),
        title=title or f"{ticker} {kind} {id}", url=url,
    )


def visible_lines(page: str) -> list[str]:
    """The page's text, one line per tag line, with the tags stripped: what you'd read."""
    lines = (re.sub(r"<[^>]+>", "", line).strip() for line in page.splitlines())
    return [line for line in lines if line]


def big_move(series, ticker=None):
    return event(f"signal:{series}", kind="big_move", ticker=ticker).model_copy(
        update={"fields": {"series": series}},
    )
