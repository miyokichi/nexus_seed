"""AT25, AT26, AT27, AT32 (spec §117–§119, §124): gaps close, or are looked at again.

Four different ways a deficiency can stop mattering, and only one of them
involves an extension:

* somebody registers the capability by an ordinary route — the gap is RESOLVED
  and nothing was constructed (spec §61);
* a proposal is approved — the gap is **not** resolved, because approving a plan
  to acquire something is not acquiring it (spec §62);
* the environment changes — the gap is analysed again, and may reach a different
  and better route than it could before (spec §69);
* the capability turns out to have been there all along — no extension is
  proposed at all (spec §124).

None of it is event replay (Invariant 52 applied to gaps): the existing gap rows
are looked at again, with their own ids and provenance intact.
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    REPOSITORY_WRITE,
    CapabilityGapStatus,
    Event,
    block,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
    register_capable,
    register_file_backend,
    reviewed,
    strategies,
)


def environment_changed(**payload) -> Event:
    return Event("extension_environment_changed", "ops", payload)


# --- AT25: resolved by an ordinary route ------------------------------------


async def test_registering_the_capability_resolves_the_gap(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    gap_id = only_gap(runtime).id

    # Somebody writes and registers the process the ordinary way.
    register_capable(runtime, "ppt_reader", ("parse_powerpoint",))
    await runtime.run_pending()

    gap = runtime.get_capability_gap(gap_id)
    assert gap.status is CapabilityGapStatus.RESOLVED
    assert gap.status.terminal
    assert runtime.get_open_capability_gaps() == []
    types = [e.type for e in runtime.event_store.all()]
    assert "capability_gap_resolved" in types
    runtime.close()


async def test_resolution_also_unblocks_the_work(tmp_path):
    """The two reconciliations are separate and agree (spec §61)."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)

    register_capable(runtime, "ppt_reader", ("parse_powerpoint",))
    await runtime.run_pending()

    assert only_gap(runtime).status is CapabilityGapStatus.RESOLVED
    assert runtime.get_work_requirement(requirement.id).status.value != (
        "BLOCKED_CAPABILITY"
    )
    runtime.close()


async def test_re_enabling_a_capability_resolves_the_gap(tmp_path):
    """The reuse case, actually carried out — by a person, not by the system."""
    from extension_helpers import register_disabled_provider

    runtime = extension_runtime(tmp_path)
    register_disabled_provider(runtime, "reporter", "generate_report")
    requirement = gap_work(runtime, required=[needs("generate_report")])
    await block(runtime, requirement)
    assert only_proposal(runtime).declared_strategy == "REGISTER_EXISTING_PROCESS"

    runtime.set_capability_enabled("generate_report", "1", True)
    await runtime.run_pending()

    assert only_gap(runtime).status is CapabilityGapStatus.RESOLVED
    runtime.close()


async def test_an_unrelated_capability_leaves_the_gap_alone(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)

    register_capable(runtime, "mailer", ("send_email",))
    await runtime.run_pending()

    assert only_gap(runtime).status is not CapabilityGapStatus.RESOLVED
    assert only_gap(runtime).missing_names == ["parse_powerpoint"]
    runtime.close()


# --- AT26: approval is not acquisition --------------------------------------


async def test_an_approved_proposal_does_not_resolve_the_gap(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    proposal = only_proposal(runtime)

    await runtime.submit_event(reviewed(proposal.id, "approve"))

    gap = only_gap(runtime)
    assert gap.status is CapabilityGapStatus.PROPOSAL_APPROVED
    assert gap.status is not CapabilityGapStatus.RESOLVED
    assert runtime.get_open_capability_gaps() == [gap]
    runtime.close()


async def test_an_approved_gap_still_resolves_when_the_capability_arrives(tmp_path):
    """And the two statuses do not fight: acquisition wins, whenever it comes."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    await runtime.submit_event(reviewed(only_proposal(runtime).id, "approve"))

    register_capable(runtime, "ppt_reader", ("parse_powerpoint",))
    await runtime.run_pending()

    assert only_gap(runtime).status is CapabilityGapStatus.RESOLVED
    runtime.close()


# --- AT27: re-analysis after the environment changes ------------------------


async def test_a_new_backend_makes_a_better_route_available(tmp_path):
    """spec §119: the same gap, re-analysed, reaches a different strategy."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(
        runtime, required=[needs("modify_repository", REPOSITORY_WRITE)]
    )
    await block(runtime, requirement)

    first = only_proposal(runtime)
    assert first.declared_strategy == "CODE_EXTENSION"  # no backend exists yet

    register_file_backend(runtime, "files")
    await runtime.submit_event(Event("backend_available", "ops", {"backend": "files"}))

    assert strategies(runtime) == ["CODE_EXTENSION", "ADD_PROCESS_DEFINITION"]
    runtime.close()


async def test_the_earlier_proposal_is_kept_and_superseded(tmp_path):
    """Invariant 92: the history of what we thought is not rewritten."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(
        runtime, required=[needs("modify_repository", REPOSITORY_WRITE)]
    )
    await block(runtime, requirement)
    first = only_proposal(runtime)
    first_waiter = next(
        c for c in runtime.continuation_store.all()
        if c.saved_process_state.get("extension_proposal_id") == str(first.id)
    )

    register_file_backend(runtime, "files")
    await runtime.submit_event(environment_changed(reason="a backend was registered"))

    kept = runtime.get_extension_proposal(first.id)
    assert kept.status.value == "SUPERSEDED"
    assert kept.declared_strategy == "CODE_EXTENSION"
    newest = runtime.get_extension_proposals()[-1]
    assert newest.declared_strategy == "ADD_PROCESS_DEFINITION"
    assert newest.status.value == "REVIEW"
    assert runtime.continuation_store.get(first_waiter.id) is None
    assert runtime.process_store.get_instance(first_waiter.process_instance_id).status.value == "COMPLETED"
    runtime.close()


async def test_re_analysis_without_a_change_proposes_nothing_new(tmp_path):
    """The same world gives the same proposal, not a second one (spec §68)."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    first = only_proposal(runtime)

    await runtime.submit_event(environment_changed(reason="nothing really"))

    assert [p.id for p in runtime.get_extension_proposals()] == [first.id]
    assert runtime.get_extension_proposal(first.id).status.value == "REVIEW"
    # And the gap is left describing where that proposal stands, not stuck
    # half-way through a re-analysis that concluded nothing had changed.
    assert only_gap(runtime).status is CapabilityGapStatus.PROPOSAL_AVAILABLE
    runtime.close()


async def test_re_analysis_leaves_an_approved_proposal_alone(tmp_path):
    """An approved proposal is a decision somebody made; it is not replaced."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(
        runtime, required=[needs("modify_repository", REPOSITORY_WRITE)]
    )
    await block(runtime, requirement)
    proposal = only_proposal(runtime)
    await runtime.submit_event(reviewed(proposal.id, "approve"))

    register_file_backend(runtime, "files")
    await runtime.submit_event(environment_changed())

    assert [p.id for p in runtime.get_extension_proposals()] == [proposal.id]
    assert runtime.get_extension_proposal(proposal.id).status.value == "APPROVED"
    runtime.close()


async def test_re_analysis_is_not_event_replay(tmp_path):
    """Invariant 52 for gaps: the same gap row, not a re-derived one."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(
        runtime, required=[needs("modify_repository", REPOSITORY_WRITE)]
    )
    await block(runtime, requirement)
    gap = only_gap(runtime)

    register_file_backend(runtime, "files")
    await runtime.submit_event(environment_changed())

    after = only_gap(runtime)
    assert after.id == gap.id
    assert after.created_at == gap.created_at
    assert after.source_match_id == gap.source_match_id
    assert len(runtime.get_work_requirements()) == 1
    runtime.close()


# --- AT32: no unnecessary extension -----------------------------------------


async def test_a_capability_found_by_reconciliation_needs_no_extension(tmp_path):
    """spec §124: a stale block, and the answer is "we can already do that"."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("generate_report")])
    register_capable(runtime, "reporter", ("generate_report",))

    # A late capability_missing from a match made before the registration.
    await runtime.submit_event(
        Event(
            "capability_missing",
            "test",
            {
                "work_requirement_id": str(requirement.id),
                "missing_capabilities": ["generate_report"],
            },
        )
    )

    assert runtime.get_extension_proposals() == []
    assert runtime.get_capability_gaps() == []
    runtime.close()


async def test_a_stale_block_on_an_open_gap_resolves_it(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("generate_report")])
    await block(runtime, requirement)
    gap_id = only_gap(runtime).id

    register_capable(runtime, "reporter", ("generate_report",))
    await runtime.submit_event(
        Event(
            "capability_missing",
            "test",
            {
                "work_requirement_id": str(requirement.id),
                "missing_capabilities": ["generate_report"],
            },
        )
    )

    assert runtime.get_capability_gap(gap_id).status is CapabilityGapStatus.RESOLVED
    assert len(runtime.get_extension_proposals()) == 1  # only the original
    runtime.close()
