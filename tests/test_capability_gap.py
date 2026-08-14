"""AT1 (spec §93): a deficiency becomes a durable record of its own.

Phase 4A could say *this work is blocked*.  What it could not say is *what we
are missing*, as a thing with an identity that survives a restart and can be
reasoned about separately from the need that revealed it (Invariant 85).
"""

from __future__ import annotations

from extension_helpers import (
    CapabilityGapStatus,
    POWERPOINT,
    WorkStatus,
    block,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    reopen,
)


async def test_blocked_work_opens_a_capability_gap(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(
        runtime, work_key="ppt:v1", required=[needs("parse_powerpoint", POWERPOINT)]
    )

    blocked = await block(runtime, requirement)

    assert blocked.status is WorkStatus.BLOCKED_CAPABILITY
    gap = only_gap(runtime)
    assert gap.work_requirement_id == requirement.id
    assert gap.missing_names == ["parse_powerpoint"]
    assert gap.reason and "parse_powerpoint" in gap.reason
    runtime.close()


async def test_the_need_itself_is_not_rewritten(tmp_path):
    """The gap is ours; the need is the world's, and it is left alone (§10)."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])

    blocked = await block(runtime, requirement)

    assert blocked.id == requirement.id
    assert blocked.work_key == requirement.work_key
    assert blocked.status is WorkStatus.BLOCKED_CAPABILITY  # not CANCELLED
    assert blocked.missing_capabilities == ["parse_powerpoint"]
    runtime.close()


async def test_the_gap_survives_a_restart(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    gap_id = only_gap(runtime).id
    missing = only_gap(runtime).missing_names
    runtime.close()

    rebuilt = reopen(tmp_path)
    restored = rebuilt.get_capability_gap(gap_id)

    assert restored is not None
    assert restored.missing_names == missing
    assert restored.work_requirement_id == requirement.id
    assert rebuilt.get_open_capability_gaps() == [restored]
    rebuilt.close()


async def test_a_gap_records_what_nearly_fits(tmp_path):
    """Partial providers: the reuse question is "who is nearly right?"."""
    runtime = extension_runtime(tmp_path)
    from extension_helpers import register_capable

    register_capable(runtime, "reader", ("read_document",))
    requirement = gap_work(
        runtime,
        required=[needs("read_document"), needs("parse_powerpoint", POWERPOINT)],
    )

    await block(runtime, requirement)

    gap = only_gap(runtime)
    assert gap.missing_names == ["parse_powerpoint"]
    assert "reader:1" in gap.current_partial_providers
    # What the work asked for as a whole is kept beside what was missing.
    assert sorted(r.name for r in gap.required_capabilities) == [
        "parse_powerpoint",
        "read_document",
    ]
    runtime.close()


async def test_composition_required_is_not_a_gap(tmp_path):
    """Every competence exists, just not together — a 4B problem, not a hole."""
    runtime = extension_runtime(tmp_path)
    from extension_helpers import register_capable

    register_capable(runtime, "extractor", ("extract",))
    register_capable(runtime, "analyzer", ("analyze",))
    requirement = gap_work(runtime, required=[needs("extract"), needs("analyze")])

    blocked = await block(runtime, requirement)

    assert blocked.status is WorkStatus.BLOCKED_CAPABILITY
    assert runtime.get_capability_gaps() == []
    assert runtime.get_extension_proposals() == []
    runtime.close()


async def test_gap_status_starts_short_of_resolved(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)

    gap = only_gap(runtime)
    assert gap.status is not CapabilityGapStatus.RESOLVED
    assert not gap.status.terminal
    runtime.close()
