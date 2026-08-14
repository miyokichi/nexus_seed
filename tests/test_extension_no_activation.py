"""AT23 (spec §115): the boundary of Phase 5A, pinned down.

This is the test that stops the phase from quietly becoming the next one.  After
a proposal has been made, validated, reviewed and **approved**, every one of the
following must still be true:

    new files                = 0
    new ProcessDefinitions   = 0
    new Capabilities         = 0
    plugin installs          = 0
    extractors registered    = 0
    adapters registered      = 0
    permissions granted      = 0
    external actions         = 0

Approval means one thing: this description may be handed to a construction
phase (spec §22).  Nothing here constructs anything.
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    REPOSITORY_WRITE,
    block,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
    register_disabled_provider,
    register_file_backend,
    reviewed,
)


def snapshot(runtime, tmp_path) -> dict:
    """Everything Phase 5A must leave alone."""
    return {
        "definitions": sorted(
            f"{d.name}:{d.version}" for d in runtime.process_store.all_definitions()
        ),
        "capabilities": sorted(str(c.ref) for c in runtime.list_capabilities()),
        "enabled": sorted(
            str(c.ref) for c in runtime.list_capabilities(enabled_only=True)
        ),
        "extractors": len(runtime.extractors),
        "backends": sorted(runtime.backends),
        "action_proposals": len(runtime.get_action_proposals()),
        "files": sorted(p.name for p in tmp_path.iterdir() if p.is_file()),
    }


async def approved_proposal(tmp_path, *, required, setup=None, name="boundary.db"):
    runtime = extension_runtime(tmp_path, name)
    if setup is not None:
        setup(runtime)
    requirement = gap_work(runtime, required=required)
    await block(runtime, requirement)
    proposal = only_proposal(runtime)
    before = snapshot(runtime, tmp_path)
    await runtime.submit_event(reviewed(proposal.id, "approve"))
    return runtime, requirement, proposal, before


async def test_an_approved_extractor_proposal_changes_nothing(tmp_path):
    runtime, requirement, proposal, before = await approved_proposal(
        tmp_path, required=[needs("parse_powerpoint", POWERPOINT)]
    )

    assert runtime.get_extension_proposal(proposal.id).status.value == "APPROVED"
    assert snapshot(runtime, tmp_path) == before
    # Specifically: the thing it proposed to add does not exist.
    assert runtime.extractors.find("structure", "powerpoint") is None
    assert runtime.get_capability("parse_powerpoint") is None
    runtime.close()


async def test_an_approved_process_proposal_registers_no_process(tmp_path):
    runtime, requirement, proposal, before = await approved_proposal(
        tmp_path,
        required=[needs("modify_repository", REPOSITORY_WRITE)],
        setup=lambda r: register_file_backend(r, "files"),
        name="process.db",
    )

    assert runtime.get_extension_proposal(proposal.id).status.value == "APPROVED"
    assert snapshot(runtime, tmp_path) == before
    assert runtime.process_store.get_definition("modify_repository_process", "1") is None
    runtime.close()


async def test_an_approved_reuse_proposal_enables_nothing(tmp_path):
    """The smallest possible extension is still not applied (spec §89)."""
    runtime, requirement, proposal, before = await approved_proposal(
        tmp_path,
        required=[needs("generate_report")],
        setup=lambda r: register_disabled_provider(r, "reporter", "generate_report"),
        name="reuse.db",
    )

    assert runtime.get_extension_proposal(proposal.id).status.value == "APPROVED"
    assert snapshot(runtime, tmp_path) == before
    assert not runtime.get_capability("generate_report", "1").enabled
    runtime.close()


async def test_the_work_is_still_unsatisfied_afterwards(tmp_path):
    """The whole point: the need stands, because we still cannot do it."""
    runtime, requirement, proposal, _before = await approved_proposal(
        tmp_path, required=[needs("parse_powerpoint", POWERPOINT)]
    )

    assert runtime.get_work_requirement(requirement.id).status.value == (
        "BLOCKED_CAPABILITY"
    )
    assert runtime.get_blocked_capability_work()[0].id == requirement.id
    assert not only_gap(runtime).status.terminal
    runtime.close()


async def test_nothing_reaches_the_outside_world(tmp_path):
    """No ActionProposal is created: self-extension is not an external act."""
    runtime, _requirement, _proposal, _before = await approved_proposal(
        tmp_path, required=[needs("parse_powerpoint", POWERPOINT)]
    )

    assert runtime.get_action_proposals() == []
    assert runtime.get_action_executions(_proposal.id) == []
    runtime.close()


async def test_health_reports_the_approved_but_unbuilt_proposal(tmp_path):
    """spec §134: the number that only grows in this phase."""
    runtime, _requirement, _proposal, _before = await approved_proposal(
        tmp_path, required=[needs("parse_powerpoint", POWERPOINT)]
    )

    health = runtime.get_extension_health()
    assert health["approved_not_constructed"] == 1
    assert health["capabilities_acquired"] == 0
    assert health["open_gaps"] == 1
    assert health["resolved_gaps"] == 0
    assert health["oldest_open_gap_at"] is not None
    runtime.close()
