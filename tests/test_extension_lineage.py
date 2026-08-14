"""AT19 (spec §111): a human edit produces a new proposal, never a rewrite.

The rule is Phase 3C's ``modify`` (spec §46) with a sharper motive.  What is
being edited here is a description of how the system would change itself, so
"what did we originally propose, and who changed it to this?" is precisely the
question a later reviewer will ask.  Overwriting proposal A with B would delete
the only record of that.
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
    reviewed,
)

#: A person's replacement: the same gap, a smaller route.
SMALLER = {
    "strategy": "ADD_EXTRACTOR",
    "title": "A minimal PowerPoint text extractor",
    "description": "text only, no charts",
    "proposed_components": [
        {
            "component_type": "RESOURCE_EXTRACTOR",
            "name": "ppt_text_extractor",
            "purpose": "extract slide text",
            "provides_capabilities": ["parse_powerpoint"],
            "required_permissions": [],
        }
    ],
    "required_permissions": ["repository.read", "process.register"],
    "estimated_risk": "MEDIUM",
    "rationale": "start with the smallest thing that could work",
}


async def modified(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    original = only_proposal(runtime)
    await runtime.submit_event(
        reviewed(original.id, "modify", replacement=SMALLER)
    )
    return runtime, original


async def test_a_modification_creates_a_second_proposal(tmp_path):
    runtime, original = await modified(tmp_path)

    proposals = runtime.get_extension_proposals()
    assert len(proposals) == 2
    replacement = proposals[-1]
    assert replacement.id != original.id
    assert replacement.title == "A minimal PowerPoint text extractor"
    assert replacement.source == "human"
    runtime.close()


async def test_the_original_is_kept_not_overwritten(tmp_path):
    runtime, original = await modified(tmp_path)

    kept = runtime.get_extension_proposal(original.id)
    assert kept.status is ExtensionProposalStatus.SUPERSEDED
    assert kept.title == original.title  # unchanged wording
    assert any("superseded by" in r for r in kept.reasons)
    runtime.close()


async def test_the_chain_is_traceable(tmp_path):
    """spec §47: root and replaces make the edit history walkable."""
    runtime, original = await modified(tmp_path)
    replacement = runtime.get_extension_proposals()[-1]

    assert replacement.replaces_proposal_id == original.id
    assert replacement.root_proposal_id == original.root_proposal_id == original.id

    trace = runtime.get_extension_trace(replacement.id)
    assert [p.id for p in trace.lineage] == [original.id, replacement.id]
    runtime.close()


async def test_a_modification_is_not_an_approval(tmp_path):
    """A human edit never yields a directly-approved extension (spec §46)."""
    runtime, original = await modified(tmp_path)
    replacement = runtime.get_extension_proposals()[-1]

    assert replacement.status is ExtensionProposalStatus.REVIEW
    assert replacement.status is not ExtensionProposalStatus.APPROVED
    # It went back through validation and policy from the top.
    decisions = runtime.get_extension_decisions(replacement.id)
    assert [d.decision.value for d in decisions] == ["REVIEW"]
    runtime.close()


async def test_the_replacement_can_then_be_approved(tmp_path):
    runtime, original = await modified(tmp_path)
    replacement = runtime.get_extension_proposals()[-1]

    await runtime.submit_event(reviewed(replacement.id, "approve"))

    assert runtime.get_extension_proposal(replacement.id).status.value == "APPROVED"
    assert runtime.get_extension_proposal(original.id).status.value == "SUPERSEDED"
    assert only_gap(runtime).status.value == "PROPOSAL_APPROVED"
    # Still no capability: approval is a handoff, not an acquisition.
    assert runtime.list_capabilities() == []
    runtime.close()


async def test_reviewing_a_superseded_proposal_decides_nothing(tmp_path):
    runtime, original = await modified(tmp_path)

    await runtime.submit_event(reviewed(original.id, "approve"))

    assert runtime.get_extension_proposal(original.id).status.value == "SUPERSEDED"
    assert [
        d.decision.value for d in runtime.get_extension_decisions(original.id)
    ] == ["REVIEW", "REJECT"]
    runtime.close()


async def test_an_unusable_replacement_is_refused(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    original = only_proposal(runtime)

    await runtime.submit_event(
        reviewed(original.id, "modify", replacement={"not": "a proposal"})
    )

    # The handler failed rather than inventing a proposal from nonsense.
    assert len(runtime.get_extension_proposals()) == 1
    assert runtime.get_extension_proposal(original.id).status is (
        ExtensionProposalStatus.REVIEW
    )
    runtime.close()


async def test_the_modified_proposal_still_has_to_be_valid(tmp_path):
    """A person may edit the description, not the boundary (spec §46)."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    original = only_proposal(runtime)

    await runtime.submit_event(
        reviewed(
            original.id,
            "modify",
            replacement={**SMALLER, "strategy": "DELETE_RUNTIME_AND_REBUILD"},
        )
    )

    replacement = runtime.get_extension_proposals()[-1]
    assert replacement.status is ExtensionProposalStatus.INVALID
    assert any("not a known extension strategy" in r for r in replacement.reasons)
    runtime.close()
