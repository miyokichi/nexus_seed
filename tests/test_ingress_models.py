"""Ingress domain models + persistence (spec §4-§6, §11-§13, §19-§20)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from nexus_seed.core.event import Event
from nexus_seed.ingress.models import (
    AdapterCheckpoint,
    IngressEnvelope,
    IngressReceipt,
    IngressStatus,
)
from nexus_seed.ingress.validation import is_keyable, validate_envelope
from nexus_seed.storage.adapter_checkpoint_store import AdapterCheckpointStore
from nexus_seed.storage.database import Database
from nexus_seed.storage.ingress_receipt_store import DuplicateIngress, IngressReceiptStore


def envelope(**kw) -> IngressEnvelope:
    defaults = dict(
        adapter_id="manual",
        source_type="cli",
        source_event_key="demo-001",
        event_type="human_message",
        payload={"text": "hello"},
    )
    defaults.update(kw)
    return IngressEnvelope(**defaults)


# --- envelope / identity ---------------------------------------------------


def test_envelope_identity_is_the_dedup_key():
    assert envelope().identity == ("manual", "demo-001")


def test_source_event_key_is_independent_of_event_id():
    """The world's name for an occurrence is not ours (Invariant 30)."""
    e = envelope()
    assert e.source_event_key == "demo-001"
    assert isinstance(e.id, uuid.UUID)
    assert str(e.id) != e.source_event_key


# --- validation ------------------------------------------------------------


def test_a_complete_envelope_validates():
    result = validate_envelope(envelope())
    assert result.ok and result.reasons == []


@pytest.mark.parametrize(
    "field",
    ["adapter_id", "source_type", "source_event_key", "event_type"],
)
def test_every_identity_field_is_required(field):
    result = validate_envelope(envelope(**{field: ""}))
    assert not result.ok
    assert f"empty {field}" in result.reasons


def test_missing_source_event_key_is_refused_not_invented():
    """Without an external identity, a redelivery could never be recognised."""
    result = validate_envelope(envelope(source_event_key=""))
    assert "empty source_event_key" in result.reasons


def test_wrongly_typed_fields_are_refused():
    result = validate_envelope(envelope(payload=[1, 2], metadata="x", observed_at="now"))
    assert not result.ok
    assert len(result.reasons) == 3


def test_keyability_decides_whether_a_rejection_can_be_recorded():
    assert is_keyable(envelope(event_type=""))
    assert not is_keyable(envelope(source_event_key=""))
    assert not is_keyable(envelope(adapter_id=""))


# --- receipt persistence ---------------------------------------------------


def test_receipt_round_trips_through_sqlite(tmp_path):
    db = Database(tmp_path / "i.db")
    store = IngressReceiptStore(db)
    event_id = uuid.uuid4()

    receipt = IngressReceipt.from_envelope(
        envelope(
            source_cursor="sha256-abc",
            metadata={"delivery": "42"},
            observed_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        ),
        status=IngressStatus.ACCEPTED,
    )
    receipt.event_id = event_id
    store.insert(receipt)

    loaded = store.get(receipt.id)
    assert loaded.adapter_id == "manual"
    assert loaded.source_event_key == "demo-001"
    assert loaded.payload == {"text": "hello"}
    assert loaded.source_cursor == "sha256-abc"
    assert loaded.metadata == {"delivery": "42"}
    assert loaded.observed_at == datetime(2026, 8, 13, tzinfo=timezone.utc)
    assert loaded.event_id == event_id
    assert loaded.status is IngressStatus.ACCEPTED
    db.close()


def test_the_database_itself_refuses_a_second_receipt(tmp_path):
    """Dedup is a constraint, not a convention (Invariant 31)."""
    db = Database(tmp_path / "i.db")
    store = IngressReceiptStore(db)
    store.insert(IngressReceipt.from_envelope(envelope()))

    with pytest.raises(DuplicateIngress):
        store.insert(IngressReceipt.from_envelope(envelope()))

    assert len(store.all()) == 1
    db.close()


def test_the_same_key_from_a_different_adapter_is_a_different_occurrence(tmp_path):
    db = Database(tmp_path / "i.db")
    store = IngressReceiptStore(db)
    store.insert(IngressReceipt.from_envelope(envelope(adapter_id="manual")))
    store.insert(IngressReceipt.from_envelope(envelope(adapter_id="webhook")))
    assert len(store.all()) == 2
    db.close()


def test_receipt_lookup_by_event_and_status(tmp_path):
    db = Database(tmp_path / "i.db")
    store = IngressReceiptStore(db)
    event_id = uuid.uuid4()
    accepted = IngressReceipt.from_envelope(envelope(), status=IngressStatus.ACCEPTED)
    accepted.event_id = event_id
    store.insert(accepted)
    store.insert(
        IngressReceipt.from_envelope(
            envelope(source_event_key="bad-001"), status=IngressStatus.REJECTED
        )
    )

    assert store.for_event(event_id).id == accepted.id
    assert store.for_event(uuid.uuid4()) is None
    assert [r.source_event_key for r in store.by_status(IngressStatus.REJECTED)] == ["bad-001"]
    assert len(store.for_adapter("manual")) == 2
    db.close()


# --- event provenance ------------------------------------------------------


def test_an_event_knows_whether_it_came_from_outside(tmp_path):
    db = Database(tmp_path / "e.db")
    from nexus_seed.storage.event_store import EventStore

    store = EventStore(db)
    receipt_id = uuid.uuid4()
    external = Event("human_message", "manual", {"text": "hi"}, ingress_receipt_id=receipt_id)
    internal = Event("state_changed", "knowledge_runtime", {})
    store.append(external)
    store.append(internal)

    assert store.get(external.id).ingress_receipt_id == receipt_id
    assert store.get(external.id).is_external is True
    assert store.get(internal.id).ingress_receipt_id is None
    assert store.get(internal.id).is_external is False
    db.close()


# --- checkpoints -----------------------------------------------------------


def test_checkpoint_round_trips_and_advances(tmp_path):
    db = Database(tmp_path / "c.db")
    store = AdapterCheckpointStore(db)

    store.save(AdapterCheckpoint("local_file", "a.txt", "sha256-1"))
    assert store.get("local_file", "a.txt").cursor == "sha256-1"

    store.save(AdapterCheckpoint("local_file", "a.txt", "sha256-2", {"change": "modified"}))
    advanced = store.get("local_file", "a.txt")
    assert advanced.cursor == "sha256-2"
    assert advanced.metadata == {"change": "modified"}
    assert len(store.for_adapter("local_file")) == 1
    db.close()


def test_checkpoints_are_scoped_per_adapter_and_stream(tmp_path):
    db = Database(tmp_path / "c.db")
    store = AdapterCheckpointStore(db)
    store.save(AdapterCheckpoint("local_file", "a.txt", "1"))
    store.save(AdapterCheckpoint("local_file", "b.txt", "2"))
    store.save(AdapterCheckpoint("other", "a.txt", "3"))

    assert store.cursors_for_adapter("local_file") == {"a.txt": "1", "b.txt": "2"}
    assert store.cursors_for_adapter("other") == {"a.txt": "3"}
    assert store.get("local_file", "missing.txt") is None

    store.delete("local_file", "a.txt")
    assert store.cursors_for_adapter("local_file") == {"b.txt": "2"}
    db.close()


def test_a_checkpoint_is_not_a_continuation(tmp_path):
    """They live in different tables and describe different things (Invariant 33)."""
    db = Database(tmp_path / "c.db")
    AdapterCheckpointStore(db).save(AdapterCheckpoint("local_file", "a.txt", "1"))

    from nexus_seed.storage.continuation_store import ContinuationStore

    assert ContinuationStore(db).all() == []
    assert not hasattr(AdapterCheckpoint("a", "b"), "resume_point")
    assert not hasattr(AdapterCheckpoint("a", "b"), "waiting_for")
    db.close()
