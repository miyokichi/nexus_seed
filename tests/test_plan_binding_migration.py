"""AT59–AT65 (spec §56–§58): a Phase 4B database, opened by a 4B.1 runtime.

Old plans name only an ``artifact_type``.  The rule is not "interpret them
generously" but the opposite: **honour them where their meaning is beyond
doubt, and refuse them where it is not** (spec §58).  Guessing would reproduce
exactly the ambiguity this phase exists to remove — and would do it on plans
nobody is looking at any more.
"""

from __future__ import annotations

import sqlite3
import uuid

from planning_helpers import (
    PlanStatus,
    make_work,
    only_plan,
    planning_runtime,
    register_chain,
    register_step,
    status_of,
    wire,
)

from nexus_seed.core.event import Event, utcnow
from nexus_seed.planning.models import PlanEdge, PlanNode, Port, ProcessPlan
from nexus_seed.planning.validation import resolve_legacy_binding
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus

#: ``plan_edges`` exactly as Phase 4B created it.
LEGACY_SCHEMA = """
CREATE TABLE plan_edges (
    id            TEXT PRIMARY KEY,
    plan_id       TEXT NOT NULL,
    from_node_id  TEXT NOT NULL,
    to_node_id    TEXT NOT NULL,
    artifact_type TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (plan_id, from_node_id, to_node_id, artifact_type)
);
"""


def legacy_database(path) -> None:
    """Create a database whose ``plan_edges`` predates bindings."""
    conn = sqlite3.connect(str(path))
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()
    conn.close()


def legacy_edge(conn, plan_id, from_id, to_id, artifact_type) -> str:
    edge_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO plan_edges (id, plan_id, from_node_id, to_node_id, "
        "artifact_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            edge_id,
            str(plan_id),
            str(from_id),
            str(to_id),
            artifact_type,
            utcnow().isoformat(),
        ),
    )
    return edge_id


# --- schema migration ------------------------------------------------------


def test_a_phase_4b_database_gains_the_binding_columns(tmp_path):
    """AT59."""
    path = tmp_path / "legacy.db"
    legacy_database(path)

    runtime = Runtime(path)

    columns = {
        row["name"]
        for row in runtime.db.conn.execute("PRAGMA table_info(plan_edges)")
    }
    assert {"output_type", "input_type", "output_key", "input_key"} <= columns
    runtime.close()


def test_the_old_uniqueness_rule_is_replaced_not_kept(tmp_path):
    """AT60: the constraint that silently dropped a second keyed edge.

    Phase 4B allowed one edge per ``(plan, producer, consumer, type)``.  A node
    producing ``m:left`` and ``m:right`` for one consumer needs two, and under
    the old rule the second vanished without an error.
    """
    path = tmp_path / "legacy.db"
    legacy_database(path)

    runtime = Runtime(path)
    plan_id, from_id, to_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for key in ("left", "right"):
        runtime.plan_store.save_edge(
            PlanEdge(
                plan_id=plan_id,
                from_node_id=from_id,
                to_node_id=to_id,
                output_type="m",
                output_key=key,
                input_type="m",
                input_key=key,
                artifact_type="m",
            )
        )

    assert len(runtime.plan_store.edges(plan_id)) == 2
    runtime.close()


def test_existing_legacy_edges_are_carried_over_by_the_migration(tmp_path):
    """AT61: rebuilding the table must not lose the rows in it."""
    path = tmp_path / "legacy.db"
    legacy_database(path)
    plan_id, from_id, to_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    conn = sqlite3.connect(str(path))
    edge_id = legacy_edge(conn, plan_id, from_id, to_id, "measurement")
    conn.commit()
    conn.close()

    runtime = Runtime(path)

    edges = runtime.plan_store.edges(plan_id)
    assert [str(e.id) for e in edges] == [edge_id]
    assert edges[0].artifact_type == "measurement"
    assert not edges[0].is_bound  # it never carried a binding
    runtime.close()


def test_the_migration_is_idempotent(tmp_path):
    """AT62: opening the same database twice must not lose or duplicate edges."""
    path = tmp_path / "legacy.db"
    legacy_database(path)
    plan_id = uuid.uuid4()

    conn = sqlite3.connect(str(path))
    legacy_edge(conn, plan_id, uuid.uuid4(), uuid.uuid4(), "measurement")
    conn.commit()
    conn.close()

    for _ in range(3):
        runtime = Runtime(path)
        assert len(runtime.plan_store.edges(plan_id)) == 1
        runtime.close()


def test_a_fresh_database_needs_no_migration(tmp_path):
    """The rebuild must not fire on a table that was created correctly."""
    runtime = Runtime(tmp_path / "fresh.db")
    tables = {
        row["name"]
        for row in runtime.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "plan_edges_legacy" not in tables
    assert not runtime.db._has_legacy_edge_constraint()
    runtime.close()


# --- interpreting an unbound edge ------------------------------------------


def test_an_unambiguous_legacy_edge_is_honoured():
    """AT63: one interpretation exists, so use it."""
    consumer = PlanNode(
        plan_id=uuid.uuid4(),
        node_key="c:v1",
        definition_name="c",
        definition_version="1",
        input_types=["measurement"],
    )
    edge = PlanEdge(
        plan_id=consumer.plan_id,
        from_node_id=uuid.uuid4(),
        to_node_id=consumer.id,
        artifact_type="measurement",
    )

    assert resolve_legacy_binding(edge, None, consumer) == Port("measurement")


def test_an_ambiguous_legacy_edge_is_refused():
    """AT64: two keyed inputs of one type, and nothing says which."""
    consumer = PlanNode(
        plan_id=uuid.uuid4(),
        node_key="c:v1",
        definition_name="c",
        definition_version="1",
        input_types=["measurement:measured", "measurement:reference"],
    )
    edge = PlanEdge(
        plan_id=consumer.plan_id,
        from_node_id=uuid.uuid4(),
        to_node_id=consumer.id,
        artifact_type="measurement",
    )

    assert resolve_legacy_binding(edge, None, consumer) is None


def test_a_bound_edge_needs_no_interpretation():
    consumer = PlanNode(
        plan_id=uuid.uuid4(),
        node_key="c:v1",
        definition_name="c",
        definition_version="1",
        input_types=["measurement:measured", "measurement:reference"],
    )
    edge = PlanEdge(
        plan_id=consumer.plan_id,
        from_node_id=uuid.uuid4(),
        to_node_id=consumer.id,
        output_type="measurement",
        output_key="measured",
        input_type="measurement",
        input_key="measured",
    )

    assert resolve_legacy_binding(edge, None, consumer) == Port("measurement", "measured")


async def test_an_ambiguous_legacy_plan_is_blocked_before_it_runs(tmp_path):
    """AT65: refused at the last possible moment, not guessed at (spec §58).

    The plan was written by a Phase 4B system and its consumer has since gained
    two keyed inputs.  Running it would mean choosing one — quietly, at
    execution time, which is the decision this phase removed.
    """
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "source", "source", ("raw",), ("measurement",))
    register_step(
        runtime,
        "sink",
        "sink",
        ("measurement:measured", "measurement:reference"),
        ("out",),
    )
    work = make_work(
        runtime, required=("source", "sink"), inputs=("raw",), outputs=("out",)
    )

    # Write the plan by hand, the way Phase 4B would have.
    plan = ProcessPlan(
        work_requirement_id=work.id,
        status=PlanStatus.VALIDATED,
        input_types=["raw"],
        required_output_types=["out"],
    )
    source = PlanNode(
        plan_id=plan.id,
        node_key="source:v1",
        definition_name="source",
        definition_version="1",
        provided_capabilities=["source"],
        input_types=["raw"],
        output_types=["measurement"],
    )
    sink = PlanNode(
        plan_id=plan.id,
        node_key="sink:v1",
        definition_name="sink",
        definition_version="1",
        provided_capabilities=["sink"],
        input_types=["measurement:measured", "measurement:reference"],
        output_types=["out"],
        depth=1,
    )
    edge = PlanEdge(
        plan_id=plan.id,
        from_node_id=source.id,
        to_node_id=sink.id,
        artifact_type="measurement",
    )
    runtime.plan_store.create(plan, [source, sink], [edge])

    await runtime.submit_event(
        Event("process_plan_created", "test", {"plan_id": str(plan.id)})
    )

    assert runtime.get_plan(plan.id).status is PlanStatus.BLOCKED
    reasons = runtime.event_store.by_type("process_plan_failed")[-1].payload["reasons"]
    assert any("ambiguous" in r for r in reasons), reasons
    assert status_of(runtime, work) is not WorkStatus.SATISFIED
    runtime.close()


async def test_an_unambiguous_legacy_plan_still_runs(tmp_path):
    """The other half of the rule: old plans are honoured where they are clear."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)

    plan = ProcessPlan(
        work_requirement_id=work.id,
        status=PlanStatus.VALIDATED,
        required_capabilities=list(work.required_capabilities),
        input_types=["raw_measurement_resource"],
        required_output_types=["analysis_report"],
    )
    nodes = []
    for depth, (name, inputs, outputs) in enumerate(
        (
            ("extract_measurement", ["raw_measurement_resource"], ["measurement"]),
            ("analyze_resistance", ["measurement"], ["resistance_analysis"]),
            (
                "generate_analysis_report",
                ["resistance_analysis"],
                ["analysis_report"],
            ),
        )
    ):
        nodes.append(
            PlanNode(
                plan_id=plan.id,
                node_key=f"{name}:v1",
                definition_name=name,
                definition_version="1",
                provided_capabilities=[name],
                input_types=inputs,
                output_types=outputs,
                depth=depth,
            )
        )
    edges = [
        PlanEdge(
            plan_id=plan.id,
            from_node_id=nodes[i].id,
            to_node_id=nodes[i + 1].id,
            artifact_type=nodes[i].output_types[0],
        )
        for i in range(2)
    ]
    runtime.plan_store.create(plan, nodes, edges)

    await runtime.submit_event(
        Event("process_plan_created", "test", {"plan_id": str(plan.id)})
    )

    assert runtime.get_plan(plan.id).status is PlanStatus.COMPLETED
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


async def test_a_legacy_plan_still_reads_back_after_a_restart(tmp_path):
    """AT61 continued: the migration does not disturb an existing plan."""
    runtime = planning_runtime(tmp_path, "mixed.db")
    register_chain(runtime)
    work = make_work(runtime)
    await runtime.submit_event(
        Event("work_required", "test", {"work_requirement_id": str(work.id)})
    )
    plan_id = only_plan(runtime).id
    before = [e.describe() for e in runtime.plan_store.edges(plan_id)]
    runtime.close()

    runtime2 = wire(Runtime(tmp_path / "mixed.db"))
    assert [e.describe() for e in runtime2.plan_store.edges(plan_id)] == before
    runtime2.close()
