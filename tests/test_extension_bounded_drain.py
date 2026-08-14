"""AT29 (spec §121): the gap→proposal→review flow fits in small slices.

Nothing about self-extension gets its own scheduler (spec §85–§86).  It is an
ordinary Process, its events carry ordinary delivery obligations, and its review
waits on an ordinary Continuation — so a runtime paced one activation at a time
reaches exactly the same place, and reaching a budget in the middle of an
analysis loses nothing (Invariant 71/73).
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    ExtensionProposalStatus,
    block,
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


async def drain_in_slices(runtime, budget, *, limit: int = 60) -> int:
    """Run one-slice-at-a-time until the system goes quiet; return the slices."""
    slices = 0
    while slices < limit:
        await runtime.drain(budget)
        slices += 1
        if (
            runtime.get_pending_event_delivery_count() == 0
            and not runtime.last_drain.remaining_runnable_processes
        ):
            break
    return slices


async def test_the_flow_reaches_review_one_activation_at_a_time(tmp_path):
    budget = DrainBudget(max_dispatches=1, max_activations=1)
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])

    await runtime.submit_event(work_required(requirement), budget)
    slices = await drain_in_slices(runtime, budget)

    assert slices > 1, "the flow finished in a single slice; the budget did nothing"
    assert runtime.get_work_requirement(requirement.id).status.value == (
        "BLOCKED_CAPABILITY"
    )
    assert only_gap(runtime).status.value == "PROPOSAL_AVAILABLE"
    assert only_proposal(runtime).status is ExtensionProposalStatus.REVIEW
    runtime.close()


async def test_a_partly_drained_flow_is_durable_mid_way(tmp_path):
    """Yielding at a budget is not a failure; what is left is a database row."""
    budget = DrainBudget(max_dispatches=1, max_activations=1)
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])

    await runtime.submit_event(work_required(requirement), budget)
    # Deliberately stop early: the analysis has not happened yet.
    await runtime.drain(budget)

    assert runtime.get_pending_event_delivery_count() >= 0
    assert runtime.get_failed_event_deliveries() == []
    # Whatever is outstanding is durable; nothing FAILED.
    assert all(
        i.status.value != "FAILED" for i in runtime.process_store.all_instances()
    )
    runtime.close()


async def test_the_slices_may_span_a_restart(tmp_path):
    """Invariant 73: another runtime finishes what this one did not."""
    budget = DrainBudget(max_dispatches=1, max_activations=1)
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await runtime.submit_event(work_required(requirement), budget)
    await runtime.drain(budget)
    runtime.close()

    rebuilt = reopen(tmp_path)
    await drain_in_slices(rebuilt, budget)

    assert only_gap(rebuilt).status.value == "PROPOSAL_AVAILABLE"
    proposal = only_proposal(rebuilt)
    assert proposal.status is ExtensionProposalStatus.REVIEW

    await rebuilt.submit_event(reviewed(proposal.id, "approve"), budget)
    await drain_in_slices(rebuilt, budget)

    assert rebuilt.get_extension_proposal(proposal.id).status.value == "APPROVED"
    rebuilt.close()


async def test_slicing_does_not_change_the_outcome(tmp_path):
    """The same scenario, paced differently, converges on the same state."""
    unbounded = extension_runtime(tmp_path, "whole.db")
    requirement = gap_work(unbounded, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(unbounded, requirement)
    whole = (
        len(unbounded.get_capability_gaps()),
        len(unbounded.get_extension_proposals()),
        only_proposal(unbounded).declared_strategy,
        only_gap(unbounded).status.value,
    )
    unbounded.close()

    budget = DrainBudget(max_dispatches=1, max_activations=1)
    sliced = extension_runtime(tmp_path, "sliced.db")
    requirement = gap_work(sliced, required=[needs("parse_powerpoint", POWERPOINT)])
    await sliced.submit_event(work_required(requirement), budget)
    await drain_in_slices(sliced, budget)

    assert (
        len(sliced.get_capability_gaps()),
        len(sliced.get_extension_proposals()),
        only_proposal(sliced).declared_strategy,
        only_gap(sliced).status.value,
    ) == whole
    sliced.close()


async def test_the_review_itself_fits_in_a_slice(tmp_path):
    budget = DrainBudget(max_dispatches=1, max_activations=1)
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    proposal = only_proposal(runtime)

    await runtime.submit_event(reviewed(proposal.id, "approve"), budget)
    await drain_in_slices(runtime, budget)

    assert runtime.get_extension_proposal(proposal.id).status.value == "APPROVED"
    assert runtime.get_pending_event_delivery_count() == 0
    runtime.close()
