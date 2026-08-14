"""AT3 (spec §95): a deficiency is traceable back to the need that revealed it.

"We are missing a PowerPoint parser" is not, by itself, actionable — the useful
question is *why do we think we need one*, and the answer has to reach the work,
the matching attempt that failed, and eventually the event outside this system
that started it.
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    block,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    reopen,
)


async def test_a_gap_names_the_match_that_found_it(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)

    gap = only_gap(runtime)
    matches = runtime.get_capability_matches(requirement.id)

    assert matches, "the capability matcher recorded no attempt"
    assert gap.source_match_id == matches[-1].id
    assert matches[-1].missing_capabilities == ["parse_powerpoint"]
    runtime.close()


async def test_the_gap_trace_reaches_the_work(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(
        runtime, work_key="ppt:v1", required=[needs("parse_powerpoint", POWERPOINT)]
    )
    await block(runtime, requirement)

    trace = runtime.get_capability_gap_trace(only_gap(runtime).id)

    assert trace is not None
    assert trace.work_requirement.id == requirement.id
    assert trace.work_requirement.work_key == "ppt:v1"
    assert trace.missing_capabilities == ["parse_powerpoint"]
    assert trace.source_match is not None
    assert trace.source_match.status.value == "MISSING_CAPABILITY"
    assert not trace.resolved
    runtime.close()


async def test_the_gap_trace_carries_what_was_proposed(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)

    trace = runtime.get_capability_gap_trace(only_gap(runtime).id)

    assert len(trace.proposals) == 1
    assert trace.latest_proposal.declared_strategy == "ADD_EXTRACTOR"
    runtime.close()


async def test_the_trace_is_rebuilt_from_storage_alone(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    gap_id = only_gap(runtime).id
    runtime.close()

    rebuilt = reopen(tmp_path)
    trace = rebuilt.get_capability_gap_trace(gap_id)

    assert trace.work_requirement.id == requirement.id
    assert trace.source_match is not None
    assert trace.latest_proposal is not None
    rebuilt.close()


async def test_an_unknown_gap_traces_to_nothing(tmp_path):
    import uuid

    runtime = extension_runtime(tmp_path)
    assert runtime.get_capability_gap_trace(uuid.uuid4()) is None
    runtime.close()
