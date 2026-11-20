import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from daily_digest_desk.models import Event
from daily_digest_desk.reasoning.summarize import KeyNumber, Summary
from daily_digest_desk.store import Store
from conftest import add_at, freeze_clock, make_event


def test_add_returns_only_new_events(store):
    first = store.add([make_event("edgar:1"), make_event("edgar:2")])
    second = store.add([make_event("edgar:2"), make_event("edgar:3")])
    assert [e.id for e in first] == ["edgar:1", "edgar:2"]
    assert [e.id for e in second] == ["edgar:3"]


def test_duplicate_within_batch_is_stored_once(store):
    new = store.add([make_event("edgar:1"), make_event("edgar:1")])
    assert len(new) == 1


def test_non_utc_timestamps_are_normalized(store):
    eastern = timezone(timedelta(hours=-4))
    e = make_event("edgar:1").model_copy(
        update={"occurred_at": datetime(2026, 10, 1, 16, 30, tzinfo=eastern)}
    )
    # model_copy skips validation, so re-validate as a source would on construction.
    e = Event.model_validate(e.model_dump())
    store.add([e])
    (got,) = store.events_inserted_since(datetime(2000, 1, 1, tzinfo=timezone.utc))
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
    with Store(path) as s:
        s.add([make_event("edgar:1")])
    with Store(path) as s:
        assert s.add([make_event("edgar:1")]) == []


# --- events_inserted_since: "new since the last digest" -------------------------

DIGEST_8AM = datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc)  # 8am New York


def test_inserted_since_is_strictly_after(store, monkeypatch):
    add_at(store, monkeypatch, DIGEST_8AM - timedelta(minutes=5), [make_event("edgar:before")])
    add_at(store, monkeypatch, DIGEST_8AM, [make_event("edgar:exactly")])  # was on the 8am page
    after = add_at(store, monkeypatch, DIGEST_8AM + timedelta(hours=6), [make_event("edgar:after")])
    assert store.events_inserted_since(DIGEST_8AM) == after


def test_inserted_since_uses_stored_time_not_occurred_time(store, monkeypatch):
    # Happened long ago, stored late (an earnings result filled in late): news to us.
    late = make_event("edgar:late", day=1)
    # Happened after the digest by occurred_at, but stored before it: already shown.
    early = make_event("edgar:early", day=30)
    add_at(store, monkeypatch, DIGEST_8AM - timedelta(hours=1), [early])
    add_at(store, monkeypatch, DIGEST_8AM + timedelta(hours=1), [late])
    assert store.events_inserted_since(DIGEST_8AM) == [late]


def test_inserted_since_orders_by_stored_time_then_id(store, monkeypatch):
    # Stored order differs from both occurred order and id order.
    first = make_event("edgar:z", day=20)
    second_b = make_event("edgar:b", day=10)
    second_a = make_event("edgar:a", day=15)
    add_at(store, monkeypatch, DIGEST_8AM + timedelta(hours=1), [first])
    # One batch shares an inserted_at, so id decides: a before b.
    add_at(store, monkeypatch, DIGEST_8AM + timedelta(hours=2), [second_b, second_a])
    assert store.events_inserted_since(DIGEST_8AM) == [first, second_a, second_b]


def test_inserted_since_round_trips_every_field(store, monkeypatch):
    event = make_event(
        "signal:big_move:finnhub:NVDA:2026-11-16", ticker="NVDA", source="signal",
        kind="big_move", title="NVDA down 6.21% to $187.40",
        url="https://finance.yahoo.com/quote/NVDA",
        fields={"series": "finnhub:NVDA", "previous": 199.82, "latest": 187.4, "move": -6.21},
    )
    add_at(store, monkeypatch, DIGEST_8AM + timedelta(hours=1), [event])
    assert store.events_inserted_since(DIGEST_8AM) == [event]  # pydantic compares every field


def test_inserted_since_nothing_new(store, monkeypatch):
    add_at(store, monkeypatch, DIGEST_8AM - timedelta(hours=1), [make_event("edgar:1")])
    assert store.events_inserted_since(DIGEST_8AM) == []


def test_inserted_since_converts_since_to_utc(store, monkeypatch):
    new_york = timezone(timedelta(hours=-5))  # EST in November
    add_at(store, monkeypatch, DIGEST_8AM, [make_event("edgar:1")])  # 13:00 UTC = 8:00am NY
    # 8:30am NY is 13:30 UTC, after the event. Compared as raw text, "08:30-05:00"
    # would sort before "13:00+00:00" and wrongly include it.
    assert store.events_inserted_since(datetime(2026, 11, 17, 8, 30, tzinfo=new_york)) == []
    assert len(store.events_inserted_since(datetime(2026, 11, 17, 7, 30, tzinfo=new_york))) == 1


def test_inserted_since_refetch_keeps_first_stored_time(store, monkeypatch):
    # EDGAR re-fetches the cursor day every run. The duplicate is ignored, and its
    # inserted_at stays the first one, so it doesn't come back on the next digest.
    add_at(store, monkeypatch, DIGEST_8AM - timedelta(hours=1), [make_event("edgar:1")])
    add_at(store, monkeypatch, DIGEST_8AM + timedelta(hours=8), [make_event("edgar:1")])
    assert store.events_inserted_since(DIGEST_8AM) == []


# --- add_summary: one checked summary per event ---------------------------------

REVENUE = KeyNumber(
    number="$57.0 billion",
    quote="Revenue was $57.0 billion, up 62% from a year ago.",
    source="EX-99.1",
)


def save(store, event_id="edgar:1", text="Revenue was $57.0 billion.", key_numbers=(REVENUE,),
         prompt_version=1, input_tokens=28_000, output_tokens=500):
    store.add_summary(
        event_id, Summary(summary=text, key_numbers=list(key_numbers)),
        model="claude-opus-5-5", prompt_version=prompt_version,
        input_tokens=input_tokens, output_tokens=output_tokens,
    )


def summary_rows(store):
    # No method reads summaries back yet, so look at the table itself.
    return store._conn.execute("SELECT * FROM summaries ORDER BY event_id").fetchall()


def test_add_summary_saves_every_column_in_its_place(store, monkeypatch):
    # The clock reads 8am New York; the store saves it as 13:00 UTC, like events.
    freeze_clock(monkeypatch, datetime(2026, 11, 17, 8, 0, tzinfo=timezone(timedelta(hours=-5))))
    # Different input and output counts, so swapping them would show.
    save(store, input_tokens=28_131, output_tokens=496)
    (row,) = summary_rows(store)
    assert row[:2] == ("edgar:1", "Revenue was $57.0 billion.")
    assert row[3:] == ("claude-opus-5-5", 1, 28_131, 496, "2026-11-17T13:00:00+00:00")


def test_add_summary_key_numbers_come_back_from_the_json(store):
    second = KeyNumber(number="1,200", quote="We added 1,200 employees.", source="EX-99.1")
    save(store, key_numbers=[REVENUE, second])
    (row,) = summary_rows(store)
    assert [KeyNumber(**d) for d in json.loads(row[2])] == [REVENUE, second]


def test_add_summary_with_no_key_numbers_saves_an_empty_list(store):
    save(store, text="Routine paperwork; nothing new about the business.", key_numbers=[])
    (row,) = summary_rows(store)
    assert row[2] == "[]"  # an empty list, not NULL, so NOT NULL holds


def test_add_summary_again_replaces_the_first(store):
    # Say, the same filing summarized again after a prompt change.
    save(store, text="First.", key_numbers=[], prompt_version=1)
    save(store, text="Second.", key_numbers=[], prompt_version=2)
    (row,) = summary_rows(store)  # one row, not two
    assert (row[1], row[4]) == ("Second.", 2)


def test_add_summary_replace_leaves_other_events_alone(store):
    save(store, event_id="edgar:1", text="One.", key_numbers=[])
    save(store, event_id="edgar:2", text="Two.", key_numbers=[])
    save(store, event_id="edgar:1", text="One again.", key_numbers=[])
    assert [(r[0], r[1]) for r in summary_rows(store)] == [
        ("edgar:1", "One again."), ("edgar:2", "Two."),
    ]


def test_add_summary_persists_to_disk(tmp_path):
    path = tmp_path / "events.db"
    with Store(path) as s:
        save(s)
    with Store(path) as s:
        assert len(summary_rows(s)) == 1
