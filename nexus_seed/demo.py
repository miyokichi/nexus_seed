"""Runnable demo of the Phase 1 acceptance scenario.

Run with::

    python -m nexus_seed.demo

It walks the full loop, including a **runtime restart** in the middle: the first
Runtime is closed after the process suspends, then a fresh Runtime is rebuilt
from the same SQLite file and drives the process to completion — proving process
state lives entirely in the database.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path

from .core.event import Event
from .core.process import ProcessStatus
from .processes.demo_resistance import bootstrap
from .runtime.runtime import Runtime


def _line(title: str) -> None:
    print(f"\n=== {title} ===")


async def run_demo(db_path: str | Path) -> None:
    """Execute the acceptance scenario against the database at ``db_path``."""
    correlation_id = uuid.uuid4()

    _line("Step 1-3: runtime #1 — parameter change, update state, suspend")
    runtime = Runtime(db_path)
    bootstrap(runtime)
    param_event = Event(
        type="process_parameter_changed",
        source="world",
        payload={"parameter": "D1_CD", "old": 48, "new": 45, "wafer": "W03"},
        correlation_id=correlation_id,
    )
    await runtime.submit_event(param_event)

    print("world state D1_CD.target =", runtime.state_store.get("D1_CD", "target"))
    instance = runtime.process_store.all_instances()[0]
    print("process instance status  =", instance.status.value)
    continuation = runtime.continuation_store.for_instance(instance.id)
    print("continuation resume_point=", continuation.resume_point)
    print("continuation waiting_for =", continuation.waiting_for)

    _line("Step 4: destroy runtime #1 — only SQLite survives")
    runtime.close()
    del runtime

    _line("Step 5-7: runtime #2 rebuilt from SQLite — measurement, resume, complete")
    runtime2 = Runtime(db_path)
    bootstrap(runtime2)
    recovered = runtime2.process_store.get_instance(instance.id)
    print("recovered status         =", recovered.status.value)
    print("recovered D1_CD.target   =", runtime2.state_store.get("D1_CD", "target"))

    measurement_event = Event(
        type="measurement_completed",
        source="metrology",
        payload={"wafer": "W03", "resistance": 123.4},
        correlation_id=correlation_id,
    )
    produced = await runtime2.submit_event(measurement_event)

    final = runtime2.process_store.get_instance(instance.id)
    print("final status             =", final.status.value)
    print("W03 resistance recorded  =", runtime2.state_store.get("measurement_W03", "resistance"))
    print("continuations remaining  =", len(runtime2.continuation_store.all()))
    print("events emitted on resume =", [e.type for e in produced])

    assert final.status is ProcessStatus.COMPLETED
    assert any(e.type == "resistance_analysis_completed" for e in produced)
    runtime2.close()

    _line("Demo complete — Event -> Process -> State -> Suspend -> restart -> Resume -> Complete")


def main() -> None:
    """Entry point: run the demo in a throwaway temporary database."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(run_demo(Path(tmp) / "nexus_demo.db"))


if __name__ == "__main__":
    main()
