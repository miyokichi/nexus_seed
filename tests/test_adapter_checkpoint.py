"""AT14 (spec §64): a pull adapter resumes where it left off.

A checkpoint answers "how much of the world have I looked at?".  It is not a
Continuation (Invariant 33) — nothing about a *process* is stored here, and
losing one costs a re-observation, not a lost piece of work.

Uses a cursor-driven pull adapter (rather than the file watcher) so the
sequential-cursor shape is exercised directly.
"""

from __future__ import annotations

from ingress_helpers import ingress

from nexus_seed.ingress.models import AdapterCheckpoint, IngressEnvelope
from nexus_seed.runtime.runtime import Runtime

STREAM = "orders"


class SequenceAdapter:
    """A pull adapter over an append-only list of items with integer revisions."""

    adapter_id = "sequence"
    source_type = "polling_api"

    def __init__(self, items: list[tuple[int, str]]) -> None:
        self.items = list(items)
        self.ingress = None
        self.reads = 0

    def bind(self, ingress_service) -> "SequenceAdapter":
        self.ingress = ingress_service
        return self

    async def poll(self) -> list[IngressEnvelope]:
        """Return everything after the persisted cursor."""
        checkpoint = self.ingress.get_checkpoint(self.adapter_id, STREAM)
        after = int(checkpoint.cursor) if checkpoint and checkpoint.cursor else 0
        self.reads += 1
        return [
            IngressEnvelope(
                adapter_id=self.adapter_id,
                source_type=self.source_type,
                source_event_key=f"order:{revision}",
                event_type="order_received",
                payload={"revision": revision, "name": name},
                source_cursor=str(revision),
            )
            for revision, name in self.items
            if revision > after
        ]

    async def poll_and_ingest(self, *, deliver: bool = True) -> list:
        results = []
        for envelope in await self.poll():
            results.append(
                await self.ingress.ingest(
                    envelope,
                    checkpoint=AdapterCheckpoint(
                        self.adapter_id, STREAM, envelope.source_cursor
                    ),
                    deliver=deliver,
                )
            )
        return results


async def test_a_pull_adapter_advances_its_cursor(tmp_path):
    runtime = Runtime(tmp_path / "seq.db")
    adapter = SequenceAdapter([(1, "a"), (2, "b")]).bind(ingress(runtime))

    await adapter.poll_and_ingest()

    assert len(runtime.event_store.all()) == 2
    assert runtime.get_adapter_checkpoint("sequence", STREAM).cursor == "2"
    runtime.close()


async def test_only_the_unprocessed_range_is_taken_after_a_restart(tmp_path):
    """AT14."""
    db_path = tmp_path / "seq.db"
    items = [(1, "a"), (2, "b"), (3, "c")]

    runtime = Runtime(db_path)
    adapter = SequenceAdapter(items[:2]).bind(ingress(runtime))
    await adapter.poll_and_ingest()
    assert len(runtime.event_store.all()) == 2
    runtime.close()

    # --- restart; the source has grown by one item ---
    runtime2 = Runtime(db_path)
    adapter2 = SequenceAdapter(items).bind(ingress(runtime2))

    pending = await adapter2.poll()
    assert [e.payload["revision"] for e in pending] == [3]

    await adapter2.poll_and_ingest()
    assert len(runtime2.event_store.all()) == 3
    assert runtime2.get_adapter_checkpoint("sequence", STREAM).cursor == "3"

    # Polling again takes nothing.
    assert await adapter2.poll() == []
    runtime2.close()


async def test_a_lost_checkpoint_costs_a_re_read_not_a_duplicate_event(tmp_path):
    """The two protections are independent; either one alone still converges."""
    db_path = tmp_path / "seq.db"
    items = [(1, "a"), (2, "b")]

    runtime = Runtime(db_path)
    SequenceAdapter(items).bind(ingress(runtime))
    adapter = SequenceAdapter(items).bind(ingress(runtime))
    await adapter.poll_and_ingest()
    runtime.close()

    runtime2 = Runtime(db_path)
    runtime2.adapter_checkpoint_store.delete("sequence", STREAM)
    adapter2 = SequenceAdapter(items).bind(ingress(runtime2))

    results = await adapter2.poll_and_ingest()

    assert [r.duplicate for r in results] == [True, True]
    assert len(runtime2.event_store.all()) == 2
    # And the checkpoint is back where it belongs.
    assert runtime2.get_adapter_checkpoint("sequence", STREAM).cursor == "2"
    runtime2.close()


async def test_checkpoints_from_different_adapters_do_not_collide(tmp_path):
    runtime = Runtime(tmp_path / "seq.db")
    service = ingress(runtime)
    service.save_checkpoint(AdapterCheckpoint("sequence", STREAM, "5"))
    service.save_checkpoint(AdapterCheckpoint("local_file", STREAM, "sha256-x"))

    assert service.get_checkpoint("sequence", STREAM).cursor == "5"
    assert service.get_checkpoint("local_file", STREAM).cursor == "sha256-x"
    assert service.get_cursors("sequence") == {STREAM: "5"}
    runtime.close()


async def test_a_checkpoint_never_runs_ahead_of_what_was_ingested(tmp_path):
    """The invariant that makes at-least-once acquisition safe (spec §21)."""
    runtime = Runtime(tmp_path / "seq.db")
    adapter = SequenceAdapter([(1, "a"), (2, "b"), (3, "c")]).bind(ingress(runtime))

    await adapter.poll_and_ingest()

    receipts = runtime.get_ingress_receipts("sequence")
    highest_ingested = max(int(r.source_cursor) for r in receipts)
    cursor = int(runtime.get_adapter_checkpoint("sequence", STREAM).cursor)
    assert cursor == highest_ingested
    runtime.close()
