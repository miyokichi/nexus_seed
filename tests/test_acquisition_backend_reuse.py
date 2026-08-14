"""AT5 (spec §97): having the tool is not having the competence.

The two things called "capability" are kept in different registries for a
reason (Phase 4A, spec §4): ``write_file`` is what a *backend* can mechanically
do, ``modify_repository`` is what a *process* can meaningfully accomplish.  When
the mechanical half already exists, the extension needed is a process — not a
second backend that would duplicate a tool the system already has (spec §72).
"""

from __future__ import annotations

from extension_helpers import (
    REPOSITORY_WRITE,
    ExtensionRisk,
    block,
    candidates_for,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
    register_file_backend,
)


async def test_an_existing_backend_makes_this_a_process_gap(tmp_path):
    """AT5: propose a process over the tool, not another tool."""
    runtime = extension_runtime(tmp_path)
    register_file_backend(runtime, "files")
    requirement = gap_work(
        runtime, required=[needs("modify_repository", REPOSITORY_WRITE)]
    )

    await block(runtime, requirement)

    proposal = only_proposal(runtime)
    assert proposal.declared_strategy == "ADD_PROCESS_DEFINITION"
    assert proposal.component_types == ["PROCESS_DEFINITION"]
    assert "backend:files" in proposal.reusable_components
    assert proposal.estimated_risk is ExtensionRisk.MEDIUM
    # No ACTION_BACKEND is proposed: that tool already exists.
    assert "ACTION_BACKEND" not in proposal.component_types
    runtime.close()


async def test_a_named_backend_is_a_connection_not_a_construction(tmp_path):
    """The smallest version of the same idea: bind to what is registered."""
    runtime = extension_runtime(tmp_path)
    register_file_backend(runtime, "files")
    requirement = gap_work(
        runtime,
        required=[
            needs(
                "modify_repository",
                {"backend": "files", "backend_actions": ["write_file"]},
            )
        ],
    )

    await block(runtime, requirement)

    proposal = only_proposal(runtime)
    assert proposal.declared_strategy == "CONNECT_EXISTING_BACKEND"
    assert proposal.component_types == ["CONFIGURATION"]
    assert proposal.estimated_risk is ExtensionRisk.LOW
    assert proposal.required_permissions == ["process.configure"]
    runtime.close()


async def test_a_missing_mechanism_is_a_bigger_extension(tmp_path):
    """No backend performs the action at all — that is new code, and HIGH."""
    runtime = extension_runtime(tmp_path)
    register_file_backend(runtime, "files")
    requirement = gap_work(
        runtime,
        required=[needs("send_email", {"backend_actions": ["send_smtp_message"]})],
    )

    await block(runtime, requirement)

    proposal = only_proposal(runtime)
    assert proposal.declared_strategy == "CODE_EXTENSION"
    assert proposal.component_types == ["ACTION_BACKEND"]
    assert proposal.estimated_risk is ExtensionRisk.HIGH
    reasons = " ".join(proposal.analysis["candidates"][0]["reasons"])
    assert "send_smtp_message" in reasons
    runtime.close()


async def test_backend_capability_is_not_semantic_capability(tmp_path):
    """Registering the tool does not close the gap (spec §28)."""
    runtime = extension_runtime(tmp_path)
    register_file_backend(runtime, "files")
    requirement = gap_work(
        runtime, required=[needs("modify_repository", REPOSITORY_WRITE)]
    )

    blocked = await block(runtime, requirement)

    # The tool is there and the work is still blocked, which is the whole point.
    assert blocked.status.value == "BLOCKED_CAPABILITY"
    assert only_gap(runtime).missing_names == ["modify_repository"]
    runtime.close()


async def test_an_unregistered_backend_is_never_assumed(tmp_path):
    """With no backends at all, nothing pretends the mechanism exists."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(
        runtime, required=[needs("modify_repository", REPOSITORY_WRITE)]
    )
    await block(runtime, requirement)

    candidates = candidates_for(runtime, only_gap(runtime))

    assert [c.strategy.value for c in candidates] == ["CODE_EXTENSION"]
    assert candidates[0].required_new_components[0].component_type == "ACTION_BACKEND"
    runtime.close()
