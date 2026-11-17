from datetime import datetime, timezone

import pytest

from daily_digest_desk import store as store_module
from daily_digest_desk.models import Event
from daily_digest_desk.store import Store


@pytest.fixture
def store():
    with Store(":memory:") as s:
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


def add_at(store, monkeypatch, when, events):
    """store.add(events), with inserted_at = `when` instead of the real now."""

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(store_module, "datetime", FrozenClock)
    return store.add(events)
