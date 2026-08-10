"""Tests for the append-only event store."""

from __future__ import annotations

import uuid

from nexus_seed.core.event import Event
from nexus_seed.storage.database import Database
from nexus_seed.storage.event_store import EventStore


def _store(tmp_path) -> EventStore:
    return EventStore(Database(tmp_path / "events.db"))


def test_append_and_get_roundtrip(tmp_path):
    store = _store(tmp_path)
    correlation = uuid.uuid4()
    event = Event(
        type="measurement_completed",
        source="metrology",
        payload={"wafer": "W03", "resistance": 123.4},
        correlation_id=correlation,
    )
    store.append(event)

    fetched = store.get(event.id)
    assert fetched is not None
    assert fetched.id == event.id
    assert fetched.type == "measurement_completed"
    assert fetched.source == "metrology"
    assert fetched.payload == {"wafer": "W03", "resistance": 123.4}
    assert fetched.correlation_id == correlation
    assert fetched.causation_id is None


def test_get_missing_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.get(uuid.uuid4()) is None


def test_query_by_type_and_correlation(tmp_path):
    store = _store(tmp_path)
    corr_a = uuid.uuid4()
    corr_b = uuid.uuid4()
    e1 = Event("a", "s", {"n": 1}, correlation_id=corr_a)
    e2 = Event("a", "s", {"n": 2}, correlation_id=corr_b)
    e3 = Event("b", "s", {"n": 3}, correlation_id=corr_a)
    for event in (e1, e2, e3):
        store.append(event)

    assert [e.payload["n"] for e in store.by_type("a")] == [1, 2]
    assert {e.payload["n"] for e in store.by_correlation(corr_a)} == {1, 3}
    assert len(store.all()) == 3


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "events.db"
    first = EventStore(Database(path))
    event = Event("persisted", "s", {"k": "v"})
    first.append(event)
    first.db.close()

    reopened = EventStore(Database(path))
    fetched = reopened.get(event.id)
    assert fetched is not None
    assert fetched.payload == {"k": "v"}
