"""AT31 (spec §123): a restart at every boundary converges on one of everything.

The same scenario, interrupted at each seam — after the block, after the gap,
after the proposal, in review — and rebuilt from SQLite each time.  What must be
true at the end is a count, because counts are what duplication breaks::

    WorkRequirement            = 1
    CapabilityGap              = 1
    logical ExtensionProposal  = 1
    review decisions           = 1
    capabilities activated     = 0
    work                       still unresolved
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    CapabilityGapStatus,
    ExtensionProposalStatus,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
    reopen,
    reviewed,
    work_required,
)

from nexus_seed.runtime.drain import DrainBudget

#: One activation at a time, so a restart can land between any two steps.
SLICE = DrainBudget(max_dispatches=1, max_activations=1)


def converged(runtime, requirement_id) -> dict:
    """The counts that would change if anything were duplicated."""
    proposals = runtime.get_extension_proposals()
    live = [p for p in proposals if p.status is not ExtensionProposalStatus.SUPERSEDED]
    gap = only_gap(runtime)
    return {
        "work": len(runtime.get_work_requirements()),
        "gaps": len(runtime.get_capability_gaps()),
        "proposals": len(live),
        "decisions": len(runtime.get_extension_decisions(live[0].id)) if live else 0,
        "capabilities": len(runtime.list_capabilities(enabled_only=True)),
        "work_status": runtime.get_work_requirement(requirement_id).status.value,
        "gap_status": gap.status.value,
        "proposal_status": live[0].status.value if live else None,
        "analyzer_instances": len(
            [
                i
                for i in runtime.process_store.all_instances()
                if i.definition_name == "analyze_capability_gap"
            ]
        ),
    }


async def run_to_quiet(runtime, *, limit: int = 60) -> None:
    for _ in range(limit):
        await runtime.drain(SLICE)
        if (
            runtime.get_pending_event_delivery_count() == 0
            and not runtime.last_drain.remaining_runnable_processes
        ):
            return


async def test_a_restart_at_every_seam_converges(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await runtime.submit_event(work_required(requirement), SLICE)

    # Interrupt repeatedly: one slice, then throw the runtime away.
    for _ in range(8):
        await runtime.drain(SLICE)
        runtime.close()
        runtime = reopen(tmp_path)

    await run_to_quiet(runtime)
    state = converged(runtime, requirement.id)

    assert state == {
        "work": 1,
        "gaps": 1,
        "proposals": 1,
        "decisions": 1,
        "capabilities": 0,
        "work_status": "BLOCKED_CAPABILITY",
        "gap_status": "PROPOSAL_AVAILABLE",
        "proposal_status": "REVIEW",
        "analyzer_instances": 1,
    }
    runtime.close()


async def test_the_review_after_all_that_still_decides_once(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await runtime.submit_event(work_required(requirement), SLICE)
    for _ in range(8):
        await runtime.drain(SLICE)
        runtime.close()
        runtime = reopen(tmp_path)
    await run_to_quiet(runtime)

    proposal = only_proposal(runtime)
    await runtime.submit_event(reviewed(proposal.id, "approve"), SLICE)
    await run_to_quiet(runtime)
    runtime.close()

    final = reopen(tmp_path)
    state = converged(final, requirement.id)

    assert state["proposals"] == 1
    assert state["proposal_status"] == "APPROVED"
    assert state["gap_status"] == CapabilityGapStatus.PROPOSAL_APPROVED.value
    # Exactly two decision rows: the policy's REVIEW and the human's APPROVE.
    assert [
        d.decision.value for d in final.get_extension_decisions(proposal.id)
    ] == ["REVIEW", "APPROVE"]
    # And after all of it, the system still cannot do the thing.
    assert state["capabilities"] == 0
    assert state["work_status"] == "BLOCKED_CAPABILITY"
    final.close()


async def test_nothing_is_left_owed_after_the_restarts(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await runtime.submit_event(work_required(requirement), SLICE)
    for _ in range(8):
        await runtime.drain(SLICE)
        runtime.close()
        runtime = reopen(tmp_path)
    await run_to_quiet(runtime)

    assert runtime.get_pending_event_delivery_count() == 0
    assert runtime.get_failed_event_deliveries() == []
    assert [
        i.status.value
        for i in runtime.process_store.all_instances()
        if i.definition_name == "analyze_capability_gap"
    ] == ["SUSPENDED"]
    runtime.close()


async def test_the_health_snapshot_agrees_after_a_restart(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await runtime.submit_event(work_required(requirement))
    before = runtime.get_extension_health()
    runtime.close()

    rebuilt = reopen(tmp_path)
    assert rebuilt.get_extension_health() == before
    assert before["awaiting_review"] == 1
    assert before["approved_not_constructed"] == 0
    assert before["capabilities_acquired"] == 0
    rebuilt.close()
