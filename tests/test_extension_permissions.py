"""AT13, AT24 (spec §105, §116): declaring a permission is not being granted one.

The permission model is Phase 3C's: flat strings granted to a
*ProcessDefinition* in its metadata, default deny.  An ExtensionProposal
declares what **construction** would need — which is information, not authority
(spec §77).  Nothing in this phase can widen anybody's grant, least of all its
own (Invariant 84).
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    REPOSITORY_WRITE,
    block,
    extension_runtime,
    gap_work,
    needs,
    only_proposal,
    register_disabled_provider,
    register_file_backend,
    reviewed,
)

from nexus_seed.actions.permissions import granted_permissions


def all_grants(runtime) -> dict[str, list[str]]:
    """Every permission grant currently recorded, by definition."""
    return {
        f"{d.name}:{d.version}": granted_permissions(d)
        for d in runtime.process_store.all_definitions()
    }


# --- AT13: what a proposal declares -----------------------------------------


async def test_a_proposal_declares_what_building_it_would_need(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])

    await block(runtime, requirement)

    proposal = only_proposal(runtime)
    assert proposal.required_permissions == ["repository.read", "process.register"]
    runtime.close()


async def test_the_declaration_scales_with_the_route(tmp_path):
    """Re-enabling something needs less than writing code (spec §76)."""
    reuse = extension_runtime(tmp_path, "reuse.db")
    register_disabled_provider(reuse, "reporter", "generate_report")
    await block(reuse, gap_work(reuse, required=[needs("generate_report")]))

    code = extension_runtime(tmp_path, "code.db")
    await block(code, gap_work(code, required=[needs("parse_unknown_binary_format")]))

    assert only_proposal(reuse).required_permissions == ["capability.enable"]
    assert "repository.modify" in only_proposal(code).required_permissions
    reuse.close()
    code.close()


async def test_the_decision_records_the_declaration_and_grants_nothing(tmp_path):
    runtime = extension_runtime(tmp_path)
    register_file_backend(runtime, "files")
    requirement = gap_work(
        runtime, required=[needs("modify_repository", REPOSITORY_WRITE)]
    )

    await block(runtime, requirement)

    proposal = only_proposal(runtime)
    decision = runtime.get_extension_decisions(proposal.id)[0]
    assert decision.required_permissions == proposal.required_permissions
    assert decision.granted_permissions == []
    runtime.close()


# --- AT24: nothing is granted -----------------------------------------------


async def test_no_grant_changes_while_a_proposal_is_made(tmp_path):
    runtime = extension_runtime(tmp_path)
    before = all_grants(runtime)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])

    await block(runtime, requirement)

    assert all_grants(runtime) == before
    runtime.close()


async def test_no_grant_changes_when_a_proposal_is_approved(tmp_path):
    """spec §116: an approved proposal still holds no permission."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    proposal = only_proposal(runtime)
    before = all_grants(runtime)

    await runtime.submit_event(reviewed(proposal.id, "approve"))

    assert runtime.get_extension_proposal(proposal.id).status.value == "APPROVED"
    assert all_grants(runtime) == before
    runtime.close()


async def test_the_analyzer_itself_is_granted_nothing(tmp_path):
    """The process that proposes extensions has no powers of its own."""
    runtime = extension_runtime(tmp_path)
    definition = runtime.process_store.get_definition("analyze_capability_gap", "1")

    assert granted_permissions(definition) == []
    runtime.close()


async def test_a_proposal_asking_for_the_keys_is_still_only_asking(tmp_path):
    """Even a CRITICAL declaration changes nothing about what is allowed."""
    runtime = extension_runtime(tmp_path)
    runtime.register_capability(
        "rewrite_permissions",
        metadata={"extension": {"component_name": "policy_patcher"}},
    )
    requirement = gap_work(runtime, required=[needs("rewrite_permissions")])
    before = all_grants(runtime)

    await block(runtime, requirement)

    proposal = only_proposal(runtime)
    assert "permission.modify" not in proposal.required_permissions
    assert all_grants(runtime) == before
    runtime.close()
