"""AT19 (spec §70, §48–§51): opening a pre-Phase-3F database.

The dangerous, tempting mistake: give every existing event a PENDING delivery
so the new guarantee applies retroactively.  That would replay the entire
history of a live system on first startup — re-interpreting messages, re-doing
work, and re-performing actions on the real world.

Legacy events are therefore backfilled as **already DELIVERED**.  The durable
guarantee begins with the events Phase 3F itself persists.
"""

from __future__ import annotations

from delivery_helpers import RECORDED, instances_named, ping, register_noter, status_of

from nexus_seed.core.event import Event
from nexus_seed.delivery.models import EventDeliveryStatus
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.storage.database import Database
from nexus_seed.storage.event_store import EventStore


def legacy_database(tmp_path, name="legacy.db", count=3):
    """A database holding events written the pre-3F way: no obligations."""
    db = Database(tmp_path / name)
    events = EventStore(db)  # no delivery store: exactly the old behaviour
    made = []
    for n in range(count):
        event = Event("ping", "legacy", {"n": n})
        events.append(event)
        made.append(event)
    db.close()
    return tmp_path / name, made


def test_the_fixture_really_has_no_delivery_records(tmp_path):
    path, made = legacy_database(tmp_path)
    db = Database(path)
    assert db.query("SELECT * FROM event_deliveries") == []
    assert len(db.query("SELECT * FROM events")) == len(made)
    db.close()


async def test_opening_a_legacy_database_backfills_without_replaying(tmp_path):
    """AT19."""
    path, made = legacy_database(tmp_path)

    runtime = Runtime(path)
    register_noter(runtime)

    deliveries = runtime.get_event_deliveries()
    assert len(deliveries) == len(made)
    assert all(d.status is EventDeliveryStatus.DELIVERED for d in deliveries)
    assert runtime.get_pending_event_delivery_count() == 0

    # And nothing is re-routed, even when asked to drain.
    await runtime.run_pending()
    assert instances_named(runtime, "noter") == []
    assert RECORDED == []
    runtime.close()


async def test_new_events_after_the_migration_are_fully_guaranteed(tmp_path):
    """The boundary is the point: old history is settled, new events are not."""
    path, made = legacy_database(tmp_path)

    runtime = Runtime(path)
    register_noter(runtime)
    fresh = ping({"n": 99})
    runtime.event_store.append(fresh)

    assert status_of(runtime, fresh.id) == "PENDING"
    assert runtime.get_pending_event_delivery_count() == 1
    runtime.close()

    # It survives a restart like any Phase 3F event.
    runtime2 = Runtime(path)
    register_noter(runtime2)
    await runtime2.run_pending()

    assert status_of(runtime2, fresh.id) == "DELIVERED"
    assert len(instances_named(runtime2, "noter")) == 1
    assert len(RECORDED) == 1
    runtime2.close()


async def test_the_backfill_is_idempotent_across_restarts(tmp_path):
    path, made = legacy_database(tmp_path)

    for _ in range(3):
        runtime = Runtime(path)
        assert len(runtime.get_event_deliveries()) == len(made)
        runtime.close()


async def test_an_outstanding_delivery_is_not_mistaken_for_legacy(tmp_path):
    """Backfill only touches events with *no* record at all."""
    path = tmp_path / "mixed.db"

    runtime = Runtime(path)
    register_noter(runtime)
    pending = ping()
    runtime.event_store.append(pending)
    runtime.close()

    runtime2 = Runtime(path)
    register_noter(runtime2)

    # Still PENDING — the backfill left it alone, so it is still owed.
    assert status_of(runtime2, pending.id) == "PENDING"
    await runtime2.run_pending()
    assert len(RECORDED) == 1
    runtime2.close()


async def test_the_schema_migration_adds_the_new_columns(tmp_path):
    """A pre-3F database gains ``process_instances.trigger_event_id``."""
    path, _ = legacy_database(tmp_path)
    db = Database(path)
    columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(process_instances)")}
    assert "trigger_event_id" in columns
    assert "event_deliveries" in {
        row["name"]
        for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    db.close()
