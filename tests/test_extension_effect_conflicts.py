"""spec §130–§132: extension effects join the existing atomic batch.

No new effect mechanism was invented (spec §130): gaps, proposals and decisions
are typed lists on ``ProcessResult`` like every layer before them, grouped under
``result.effects.extension``.  Which means they inherit the two properties that
matter — they commit in one transaction with everything else, and two staged
writes that contradict each other fail the activation rather than being resolved
by list order (Invariant 65).

A gap told to be both RESOLVED and OPEN in one activation is exactly the sort of
silent last-write-wins that Phase 4A shipped once already.
"""

from __future__ import annotations

import uuid

import pytest
from extension_helpers import (
    POWERPOINT,
    block,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
)

from nexus_seed.core.effects import EffectConflictError, check_conflicts
from nexus_seed.core.process import ProcessResult, ProcessStatus
from nexus_seed.extension.models import CapabilityGapStatus, ExtensionProposalStatus


def result(**kwargs) -> ProcessResult:
    return ProcessResult(status=ProcessStatus.COMPLETED, **kwargs)


# --- conflicts --------------------------------------------------------------


def test_contradictory_gap_updates_fail_the_activation():
    gap_id = uuid.uuid4()
    staged = result(
        capability_gap_updates=[(gap_id, "RESOLVED"), (gap_id, "OPEN")]
    )

    with pytest.raises(EffectConflictError) as exc:
        check_conflicts(staged)

    assert "conflicting capability gap updates" in str(exc.value)
    assert str(gap_id) in str(exc.value)


def test_identical_gap_updates_collapse():
    gap_id = uuid.uuid4()
    staged = result(capability_gap_updates=[(gap_id, "RESOLVED"), (gap_id, "RESOLVED")])

    check_conflicts(staged)

    assert staged.capability_gap_updates == [(gap_id, "RESOLVED")]


def test_contradictory_proposal_updates_fail_the_activation():
    proposal_id = uuid.uuid4()
    staged = result(
        extension_proposal_updates=[
            (proposal_id, "APPROVED", []),
            (proposal_id, "REJECTED", []),
        ]
    )

    with pytest.raises(EffectConflictError) as exc:
        check_conflicts(staged)

    assert "conflicting extension proposal updates" in str(exc.value)


def test_identical_proposal_updates_collapse():
    proposal_id = uuid.uuid4()
    staged = result(
        extension_proposal_updates=[
            (proposal_id, "SUPERSEDED", ["replaced"]),
            (proposal_id, "SUPERSEDED", ["replaced"]),
        ]
    )

    check_conflicts(staged)

    assert staged.extension_proposal_updates == [(proposal_id, "SUPERSEDED", ["replaced"])]


def test_updates_to_different_records_are_left_alone():
    a, b = uuid.uuid4(), uuid.uuid4()
    staged = result(capability_gap_updates=[(a, "RESOLVED"), (b, "OPEN")])
    check_conflicts(staged)
    assert staged.capability_gap_updates == [(a, "RESOLVED"), (b, "OPEN")]


# --- the grouping view ------------------------------------------------------


def test_extension_effects_are_grouped_and_typed():
    """spec §130: a typed group, never a generic Effect(type, payload)."""
    staged = result(
        capability_gaps=["gap"],
        capability_gap_updates=[(uuid.uuid4(), "OPEN")],
        extension_proposals=["proposal"],
        extension_decisions=["decision"],
    )

    effects = staged.effects

    assert effects.extension.gaps == ["gap"]
    assert effects.extension.proposals == ["proposal"]
    assert effects.extension.decisions == ["decision"]
    assert effects.extension.any
    assert not effects.is_empty


def test_an_activation_with_no_extension_effects_is_still_empty():
    assert result().effects.is_empty
    assert not result().effects.extension.any


# --- atomicity --------------------------------------------------------------


async def test_the_gap_and_its_proposal_land_together(tmp_path):
    """spec §132: a crash cannot leave a proposal about a gap that is not there."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])

    await block(runtime, requirement)

    gap = only_gap(runtime)
    proposal = only_proposal(runtime)
    assert proposal.capability_gap_id == gap.id
    assert gap.status is CapabilityGapStatus.PROPOSAL_AVAILABLE
    assert proposal.status is ExtensionProposalStatus.REVIEW
    # One activation wrote all three: gap, proposal and decision.
    assert len(runtime.get_extension_decisions(proposal.id)) == 1
    runtime.close()
