"""AT17 (spec §73, §45): the operator's view of a matching decision.

From a work requirement: what it needed, who was considered, what was chosen,
and on what grounds — resolved from storage alone.
"""

from __future__ import annotations

import uuid

from capability_helpers import (
    capability_runtime,
    make_work,
    offer_work,
    register_capable,
)

from nexus_seed.runtime.runtime import Runtime


async def test_a_trace_explains_a_successful_match(tmp_path):
    """AT17."""
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "plain", ("analyze_resistance",))
    register_capable(runtime, "preferred", ("analyze_resistance",), priority=10)
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))

    await offer_work(runtime, work)

    trace = runtime.get_capability_trace(work.id)
    assert trace.required_capabilities == ["analyze_resistance"]
    assert trace.selected == ("preferred", "1")
    assert trace.missing_capabilities == []
    assert {c["definition_name"] for c in trace.candidates} == {"plain", "preferred"}
    assert any("chose preferred" in r for r in trace.reasons)
    assert not trace.was_blocked
    # And the process that actually ran is on the trace.
    assert [i.definition_name for i in trace.instances] == ["preferred"]
    runtime.close()


async def test_a_trace_explains_why_work_is_waiting(tmp_path):
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "partial", ("a",))
    work = make_work(runtime, work_key="w1", required=("a", "z"))

    await offer_work(runtime, work)

    trace = runtime.get_capability_trace(work.id)
    assert trace.required_capabilities == ["a", "z"]
    assert trace.missing_capabilities == ["z"]
    assert trace.selected is None
    assert trace.was_blocked
    assert trace.instances == []
    assert "no process provides ['z']" in trace.reasons[0]
    runtime.close()


async def test_a_trace_shows_the_whole_history_after_reconciliation(tmp_path):
    runtime = capability_runtime(tmp_path)
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, work)

    register_capable(runtime, "analyzer", ("analyze_resistance",))
    await runtime.run_pending()

    trace = runtime.get_capability_trace(work.id)
    assert len(trace.attempts) == 2
    assert trace.was_blocked  # it was, once
    assert trace.selected == ("analyzer", "1")  # but the latest attempt matched
    assert trace.latest.status.value == "MATCHED_SINGLE_PROCESS"
    runtime.close()


async def test_a_trace_of_legacy_work_is_empty_but_valid(tmp_path):
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    work = make_work(runtime, work_key="w1", required=())

    await offer_work(runtime, work)

    trace = runtime.get_capability_trace(work.id)
    assert trace is not None
    assert trace.required_capabilities == []
    assert trace.attempts == []
    assert trace.selected is None
    assert not trace.was_blocked
    runtime.close()


def test_an_unknown_requirement_has_no_trace(tmp_path):
    runtime = Runtime(tmp_path / "t.db")
    assert runtime.get_capability_trace(uuid.uuid4()) is None
    runtime.close()


async def test_operational_queries_answer_what_can_this_system_do(tmp_path):
    """Spec §94."""
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    register_capable(runtime, "reporter", ("generate_analysis_report",))
    blocked = make_work(runtime, work_key="w1", required=("summarize_report",))
    await offer_work(runtime, blocked)

    assert sorted(c.name for c in runtime.list_capabilities(enabled_only=True)) == [
        "analyze_resistance",
        "generate_analysis_report",
    ]
    assert [c.name for c in runtime.get_process_capabilities("analyzer", "1")] == [
        "analyze_resistance"
    ]
    assert [w.work_key for w in runtime.get_blocked_capability_work()] == ["w1"]

    candidates = runtime.find_capable_processes(["analyze_resistance"])
    assert {c.definition_name for c in candidates if c.eligible} == {"analyzer"}
    runtime.close()
