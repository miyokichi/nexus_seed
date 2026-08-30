"""Phase K2 — World Projection: Knowledge Ledger -> World View -> diff."""

from __future__ import annotations

from datetime import datetime, timezone

from nexus_seed.core.event import Event
from nexus_seed.knowledge import KnowledgeLedger
from nexus_seed.knowledge.projection import (
    WorldStateProjection,
    annotate_world_fact,
    diff_world_views,
)
from nexus_seed.storage import Database, EventStore, KnowledgeStore


def _ledger(tmp_path) -> tuple[KnowledgeLedger, Database]:
    db = Database(tmp_path / "k.db")
    return KnowledgeLedger(KnowledgeStore(db)), db


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


async def test_projection_reads_only_annotated_facts(tmp_path):
    ledger, _ = _ledger(tmp_path)
    raw = ledger.record("B案の方がmarginはありそう", source_type="meeting")
    annotate_world_fact(ledger, raw.knowledge_id, entity="project-A", attribute="margin", value="sufficient")
    ledger.record("これはただの雑談", source_type="chat")  # never annotated

    projection = WorldStateProjection(ledger)
    view = projection.view()

    assert view.get("project-A", "margin") == "sufficient"
    assert view.snapshot() == {"project-A": {"margin": "sufficient"}}


async def test_projection_as_of_past_transaction_time(tmp_path):
    ledger, _ = _ledger(tmp_path)
    raw = ledger.record(
        "margin sufficient", source_type="meeting", recorded_at=dt("2026-08-10T00:00:00")
    )
    annotate_world_fact(
        ledger, raw.knowledge_id, entity="project-A", attribute="margin", value="sufficient",
        recorded_at=dt("2026-08-11T00:00:00"),
    )
    t_checkpoint = dt("2026-08-15T00:00:00")
    ledger.revise(raw.knowledge_id, value="margin insufficient", recorded_at=dt("2026-08-20T00:00:00"))
    annotate_world_fact(
        ledger, raw.knowledge_id, entity="project-A", attribute="margin", value="insufficient",
        recorded_at=dt("2026-08-20T00:01:00"),
    )

    projection = WorldStateProjection(ledger)
    assert projection.view().get("project-A", "margin") == "insufficient"
    assert projection.view(as_of=t_checkpoint).get("project-A", "margin") == "sufficient"


async def test_projection_valid_at_uses_real_world_time(tmp_path):
    ledger, _ = _ledger(tmp_path)
    raw = ledger.record(
        "owner: suzuki",
        source_type="hr_system",
        recorded_at=dt("2026-08-01T00:00:00"),
        valid_from=dt("2026-08-01T00:00:00"),
    )
    annotate_world_fact(ledger, raw.knowledge_id, entity="project-A", attribute="owner", value="suzuki")
    ledger.revise(
        raw.knowledge_id,
        value="owner: sato",
        recorded_at=dt("2026-08-20T00:00:00"),
        valid_from=dt("2026-08-10T00:00:00"),
    )
    annotate_world_fact(ledger, raw.knowledge_id, entity="project-A", attribute="owner", value="sato")

    projection = WorldStateProjection(ledger)
    # In reality the owner changed on 8/10, even though we only found out on 8/20.
    assert projection.view(valid_at=dt("2026-08-05T00:00:00")).get("project-A", "owner") == "suzuki"
    assert projection.view(valid_at=dt("2026-08-12T00:00:00")).get("project-A", "owner") == "sato"


async def test_conflicting_facts_are_reported_not_hidden(tmp_path):
    ledger, _ = _ledger(tmp_path)
    a = ledger.record("deadline: Sep 1", source_type="email", source_ref="A")
    annotate_world_fact(ledger, a.knowledge_id, entity="project-A", attribute="deadline", value="Sep 1")
    b = ledger.record("deadline: Sep 15", source_type="email", source_ref="B")
    annotate_world_fact(ledger, b.knowledge_id, entity="project-A", attribute="deadline", value="Sep 15")

    view = WorldStateProjection(ledger).view()
    assert view.is_conflicted("project-A", "deadline")
    values = {f.value for f in view.conflicts[("project-A", "deadline")]}
    assert values == {"Sep 1", "Sep 15"}


async def test_diff_detects_added_changed_removed(tmp_path):
    ledger, _ = _ledger(tmp_path)
    projection = WorldStateProjection(ledger)
    before = projection.view()

    margin = ledger.record("margin insufficient", source_type="meeting")
    annotate_world_fact(ledger, margin.knowledge_id, entity="project-A", attribute="margin", value="insufficient")
    owner = ledger.record("owner: tanaka", source_type="meeting")
    annotate_world_fact(ledger, owner.knowledge_id, entity="project-A", attribute="owner", value="tanaka")

    after = projection.view()
    diff = diff_world_views(before, after)
    added = {(c.entity, c.attribute, c.new_value) for c in diff.changes if c.change == "added"}
    assert added == {("project-A", "margin", "insufficient"), ("project-A", "owner", "tanaka")}

    before2 = after
    ledger.revise(margin.knowledge_id, value="margin sufficient after all")
    annotate_world_fact(ledger, margin.knowledge_id, entity="project-A", attribute="margin", value="sufficient")
    after2 = projection.view()
    diff2 = diff_world_views(before2, after2)
    assert len(diff2.changes) == 1
    change = diff2.changes[0]
    assert change.change == "changed"
    assert change.old_value == "insufficient" and change.new_value == "sufficient"


async def test_diff_connects_to_existing_event_driven_loop(tmp_path):
    """Diff renders as ordinary state_changed Events the existing runtime consumes."""
    ledger, db = _ledger(tmp_path)
    event_store = EventStore(db)
    projection = WorldStateProjection(ledger)

    before = projection.view()
    margin = ledger.record("margin insufficient", source_type="meeting")
    annotate_world_fact(ledger, margin.knowledge_id, entity="project-A", attribute="margin", value="insufficient")
    after = projection.view()

    diff = diff_world_views(before, after)
    events = diff.to_events(source="knowledge_runtime")
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, Event)
    assert event.type == "state_changed"
    assert event.payload["entity"] == "project-A"
    assert event.payload["attribute"] == "margin"
    assert event.payload["new_value"] == "insufficient"

    event_store.append(event)
    assert event_store.get(event.id) is not None


async def test_existing_world_state_api_untouched(tmp_path):
    """The Phase 2B StateStore keeps working exactly as before (no coupling)."""
    from nexus_seed.storage import StateStore

    db = Database(tmp_path / "s.db")
    state = StateStore(db)
    state.set("D1_CD", "target", 42.0)
    assert state.get("D1_CD", "target") == 42.0
    assert len(state.get_history("D1_CD", "target")) == 1
