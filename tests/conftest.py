from datetime import datetime, timezone

import pytest

from daily_digest_desk.events import Event
from daily_digest_desk.store import EventStore


@pytest.fixture
def store():
    with EventStore(":memory:") as s:
        yield s


def make_event(id: str = "edgar:1", ticker: str = "AAPL", day: int = 1, **kw) -> Event:
    return Event(
        id=id,
        source=kw.pop("source", "edgar"),
        kind=kw.pop("kind", "8-K"),
        ticker=ticker,
        occurred_at=datetime(2026, 10, day, 20, 0, tzinfo=timezone.utc),
        title=kw.pop("title", f"{ticker} event {id}"),
        url=kw.pop("url", "https://example.test"),
        **kw,
    )
