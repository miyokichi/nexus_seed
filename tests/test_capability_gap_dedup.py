"""AT2 (spec §94): one deficiency, however many times it is noticed.

The identity of a gap is *this need, missing exactly this set* (spec §127) — not
the row that happened to be written first, and not the event that revealed it.
Delivery is at-least-once, so a redelivered ``capability_missing`` is a normal
occurrence, and it must find the gap instead of opening a second one.
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    Event,
    block,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
)

from nexus_seed.extension.models import CapabilityGap, missing_key_for


def missing_event(requirement, names) -> Event:
    """A second, independent ``capability_missing`` for the same need."""
    return Event(
        "capability_missing",
        "test",
        {
            "work_requirement_id": str(requirement.id),
            "work_type": requirement.work_type,
            "missing_capabilities": list(names),
        },
    )


async def test_a_redelivered_block_finds_the_same_gap(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    first = only_gap(runtime)

    await runtime.submit_event(missing_event(requirement, ["parse_powerpoint"]))

    assert only_gap(runtime).id == first.id
    runtime.close()


async def test_a_redelivered_block_does_not_re_propose(tmp_path):
    """Idempotency reaches the proposal too (spec §67)."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    first = only_proposal(runtime)

    await runtime.submit_event(missing_event(requirement, ["parse_powerpoint"]))
    await runtime.submit_event(missing_event(requirement, ["parse_powerpoint"]))

    assert only_proposal(runtime).id == first.id
    assert len(runtime.get_extension_decisions(first.id)) == 1
    runtime.close()


async def test_a_different_missing_set_is_a_different_gap(tmp_path):
    """A changed deficiency is a new fact, not an edit of the old one (§11)."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(
        runtime,
        required=[needs("parse_powerpoint", POWERPOINT), needs("summarize_slides")],
    )
    await block(runtime, requirement)
    first = only_gap(runtime)
    assert sorted(first.missing_names) == ["parse_powerpoint", "summarize_slides"]

    await runtime.submit_event(missing_event(requirement, ["parse_powerpoint"]))

    gaps = runtime.get_capability_gaps()
    assert len(gaps) == 2
    assert {tuple(sorted(g.missing_names)) for g in gaps} == {
        ("parse_powerpoint", "summarize_slides"),
        ("parse_powerpoint",),
    }
    runtime.close()


def test_the_dedup_key_ignores_order(tmp_path):
    """Two orderings of the same missing set are one deficiency."""
    forwards = missing_key_for([needs("a"), needs("b")])
    backwards = missing_key_for([needs("b"), needs("a")])
    assert forwards == backwards


def test_the_dedup_key_separates_versions():
    """Needing v2 of something is not the same hole as needing any version."""
    from nexus_seed.capabilities.models import CapabilityRequirement

    any_version = missing_key_for([CapabilityRequirement(name="a")])
    pinned = missing_key_for([CapabilityRequirement(name="a", version_constraint="2")])
    assert any_version != pinned


async def test_the_store_refuses_a_second_row_for_one_deficiency(tmp_path):
    """The database enforces it, not only the handler (spec §127)."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    first = only_gap(runtime)

    duplicate = CapabilityGap(
        work_requirement_id=requirement.id,
        missing_capabilities=list(first.missing_capabilities),
    )
    runtime.extension_store.save_gap(duplicate)

    assert len(runtime.get_capability_gaps()) == 1
    assert runtime.get_capability_gap(duplicate.id) is None
    runtime.close()
