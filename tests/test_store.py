from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from daily_digest_desk.events import Event
from daily_digest_desk.store import EventStore
from conftest import make_event


def test_add_returns_only_new_events(store):
    first = store.add([make_event("edgar:1"), make_event("edgar:2")])
    second = store.add([make_event("edgar:2"), make_event("edgar:3")])
    assert [e.id for e in first] == ["edgar:1", "edgar:2"]
    assert [e.id for e in second] == ["edgar:3"]


def test_duplicate_within_batch_is_stored_once(store):
    new = store.add([make_event("edgar:1"), make_event("edgar:1")])
    assert len(new) == 1


def test_events_since_round_trips_and_orders(store):
    store.add([
        make_event("edgar:b", day=3, fields={"items": ["2.02"]}),
        make_event("edgar:a", day=2),
        make_event("edgar:old", day=1),
    ])
    got = store.events_since(datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert [e.id for e in got] == ["edgar:a", "edgar:b"]
    assert got[1].fields == {"items": ["2.02"]}
    assert got[1].occurred_at == datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)


def test_events_since_filters_tickers(store):
    store.add([make_event("edgar:1", ticker="AAPL"), make_event("edgar:2", ticker="NVDA")])
    got = store.events_since(datetime(2026, 1, 1, tzinfo=timezone.utc), tickers=["NVDA"])
    assert [e.ticker for e in got] == ["NVDA"]


def test_non_utc_timestamps_are_normalized(store):
    eastern = timezone(timedelta(hours=-4))
    e = make_event("edgar:1").model_copy(
        update={"occurred_at": datetime(2026, 10, 1, 16, 30, tzinfo=eastern)}
    )
    # model_copy skips validation, so re-validate as a source would on construction.
    e = Event.model_validate(e.model_dump())
    store.add([e])
    (got,) = store.events_since(datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert got.occurred_at == datetime(2026, 10, 1, 20, 30, tzinfo=timezone.utc)


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError):
        Event(id="x", source="s", kind="k", ticker=None,
              occurred_at=datetime(2026, 10, 1), title="t", url="u")


def test_cursor_upsert(store):
    assert store.get_cursor("edgar:AAPL") is None
    store.set_cursor("edgar:AAPL", "2026-10-30")
    store.set_cursor("edgar:AAPL", "2026-10-31")
    assert store.get_cursor("edgar:AAPL") == "2026-10-31"


def test_persists_to_disk(tmp_path):
    path = tmp_path / "sub" / "events.db"
    with EventStore(path) as s:
        s.add([make_event("edgar:1")])
    with EventStore(path) as s:
        assert s.add([make_event("edgar:1")]) == []
