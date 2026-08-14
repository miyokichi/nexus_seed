"""AT14, AT15, AT92 (spec §106, §107, §92): what may proceed without a person.

Phase 5A is deliberately conservative (spec §43).  This is the phase in which
the system first describes changes to itself, and how much autonomy that gets is
a Phase 5D question — starting permissive would answer it by accident.

The three scenarios of spec §91 (reuse / new process / new code) must reach
*different* risk levels, or the classification is decorative.
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    REPOSITORY_WRITE,
    ExtensionPolicy,
    ExtensionProposalStatus,
    ExtensionRisk,
    ExtensionStrategy,
    block,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
    register_disabled_provider,
    register_file_backend,
)

from nexus_seed.extension.models import ExtensionDecision


# --- the table --------------------------------------------------------------


def test_the_default_sends_everything_to_a_person():
    policy = ExtensionPolicy()
    assert policy.decide(ExtensionRisk.LOW) is ExtensionDecision.REVIEW
    assert policy.decide(ExtensionRisk.MEDIUM) is ExtensionDecision.REVIEW
    assert policy.decide(ExtensionRisk.HIGH) is ExtensionDecision.REVIEW
    assert policy.decide(ExtensionRisk.CRITICAL) is ExtensionDecision.REJECT


def test_an_invalid_proposal_is_refused_whatever_the_table_says():
    policy = ExtensionPolicy(
        decisions={r: ExtensionDecision.APPROVE for r in ExtensionRisk},
        require_human_approval=False,
    )
    assert policy.decide(ExtensionRisk.LOW, valid=False) is ExtensionDecision.REJECT


def test_human_approval_overrides_an_approve_entry():
    """Invariant 91: the policy may be relaxed, but not by accident."""
    policy = ExtensionPolicy(
        decisions={ExtensionRisk.LOW: ExtensionDecision.APPROVE},
        require_human_approval=True,
    )
    assert policy.decide(ExtensionRisk.LOW) is ExtensionDecision.REVIEW

    permissive = ExtensionPolicy(
        decisions={ExtensionRisk.LOW: ExtensionDecision.APPROVE},
        require_human_approval=False,
    )
    assert permissive.decide(ExtensionRisk.LOW) is ExtensionDecision.APPROVE


def test_an_unmapped_risk_falls_back_to_review():
    policy = ExtensionPolicy(decisions={}, require_human_approval=False)
    assert policy.decide(ExtensionRisk.CRITICAL) is ExtensionDecision.REVIEW


def test_the_policy_round_trips_through_definition_metadata():
    policy = ExtensionPolicy(
        decisions={
            ExtensionRisk.LOW: ExtensionDecision.APPROVE,
            ExtensionRisk.CRITICAL: ExtensionDecision.REJECT,
        },
        allowed_strategies=(ExtensionStrategy.REGISTER_EXISTING_PROCESS,),
        require_human_approval=False,
    )
    restored = ExtensionPolicy.from_dict(policy.to_dict())
    assert restored.decide(ExtensionRisk.LOW) is ExtensionDecision.APPROVE
    assert restored.allows(ExtensionStrategy.REGISTER_EXISTING_PROCESS)
    assert not restored.allows(ExtensionStrategy.CODE_EXTENSION)
    assert not restored.require_human_approval


def test_garbage_metadata_gives_the_conservative_default():
    for data in (None, {}, {"decisions": "yes"}, ["nonsense"]):
        policy = ExtensionPolicy.from_dict(data)
        assert policy.decide(ExtensionRisk.CRITICAL) is ExtensionDecision.REJECT
        assert policy.require_human_approval


def test_unsupported_can_never_be_allowed():
    policy = ExtensionPolicy.from_dict(
        {"allowed_strategies": ["UNSUPPORTED", "ADD_EXTRACTOR"]}
    )
    assert not policy.allows(ExtensionStrategy.UNSUPPORTED)
    assert policy.allows(ExtensionStrategy.ADD_EXTRACTOR)


# --- AT92: the three scenarios differ ---------------------------------------


async def test_reuse_new_process_and_new_code_are_classified_apart(tmp_path):
    """spec §91–§92: A, B and C must not all land on the same level."""
    reuse = extension_runtime(tmp_path, "a.db")
    register_disabled_provider(reuse, "report_generator", "generate_report")
    await block(reuse, gap_work(reuse, required=[needs("generate_report")]))
    a = only_proposal(reuse)

    process = extension_runtime(tmp_path, "b.db")
    register_file_backend(process, "files")
    await block(
        process,
        gap_work(process, required=[needs("modify_repository", REPOSITORY_WRITE)]),
    )
    b = only_proposal(process)

    code = extension_runtime(tmp_path, "c.db")
    await block(code, gap_work(code, required=[needs("parse_unknown_binary_format")]))
    c = only_proposal(code)

    assert a.declared_strategy == "REGISTER_EXISTING_PROCESS"
    assert b.declared_strategy == "ADD_PROCESS_DEFINITION"
    assert c.declared_strategy == "CODE_EXTENSION"
    assert (a.estimated_risk, b.estimated_risk, c.estimated_risk) == (
        ExtensionRisk.LOW,
        ExtensionRisk.MEDIUM,
        ExtensionRisk.HIGH,
    )
    for runtime in (reuse, process, code):
        runtime.close()


# --- AT15: a medium proposal waits for a person -----------------------------


async def test_a_new_process_proposal_suspends_for_review(tmp_path):
    runtime = extension_runtime(tmp_path)
    register_file_backend(runtime, "files")
    requirement = gap_work(
        runtime, required=[needs("modify_repository", REPOSITORY_WRITE)]
    )

    await block(runtime, requirement)

    proposal = only_proposal(runtime)
    assert proposal.status is ExtensionProposalStatus.REVIEW
    assert proposal.human_approval_required
    assert only_gap(runtime).status.value == "PROPOSAL_AVAILABLE"

    # The continuation is an ordinary one, and it is durable (spec §44).
    instances = [
        i
        for i in runtime.process_store.all_instances()
        if i.definition_name == "analyze_capability_gap"
    ]
    assert len(instances) == 1
    continuation = runtime.continuation_store.for_instance(instances[0].id)
    assert continuation is not None
    assert continuation.waiting_for["event_type"] == "extension_reviewed"
    assert continuation.waiting_for["proposal_id"] == str(proposal.id)

    types = [e.type for e in runtime.event_store.all()]
    assert "extension_proposed" in types
    assert "extension_review_required" in types
    assert "extension_approved" not in types
    runtime.close()


# --- AT14: a critical proposal is never auto-approved ------------------------


async def test_a_critical_proposal_is_refused_not_approved(tmp_path):
    """spec §106: asking for runtime.modify does not get waved through."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(
        runtime,
        required=[
            needs(
                "rewrite_runtime",
                {"component_name": "runtime_patcher"},
            )
        ],
    )
    # A capability whose acquisition would need the keys to the kingdom.
    runtime.register_capability(
        "rewrite_runtime",
        metadata={"extension": {"component_name": "runtime_patcher"}},
    )
    await block(runtime, requirement)

    proposal = only_proposal(runtime)
    # The route is new code; what makes it critical is what it would need.
    assert proposal.declared_strategy == "CODE_EXTENSION"
    assert proposal.status is not ExtensionProposalStatus.APPROVED

    # And the same proposal with a rule-changing permission is CRITICAL.
    from nexus_seed.extension.strategies import classify_risk

    assert (
        classify_risk(
            ExtensionStrategy.CODE_EXTENSION, permissions=["runtime.modify"]
        )
        is ExtensionRisk.CRITICAL
    )
    runtime.close()


async def test_a_policy_that_forbids_new_code_refuses_it(tmp_path):
    """A deployment that never wants code written says so once, in data."""
    policy = ExtensionPolicy(
        allowed_strategies=(
            ExtensionStrategy.REGISTER_EXISTING_PROCESS,
            ExtensionStrategy.CONFIGURE_EXISTING_PROCESS,
        )
    )
    runtime = extension_runtime(tmp_path, policy=policy)
    requirement = gap_work(runtime, required=[needs("parse_unknown_binary_format")])

    await block(runtime, requirement)

    proposal = only_proposal(runtime)
    assert proposal.status is ExtensionProposalStatus.INVALID
    assert any("not permitted by policy" in r for r in proposal.reasons)
    # The gap stands; only this way of closing it was refused.
    assert only_gap(runtime).status.value == "OPEN"
    runtime.close()


async def test_the_policy_survives_a_restart_on_the_definition(tmp_path):
    """Policy is data on the definition, not a constructor argument (spec §42)."""
    policy = ExtensionPolicy(
        allowed_strategies=(ExtensionStrategy.REGISTER_EXISTING_PROCESS,)
    )
    runtime = extension_runtime(tmp_path, policy=policy)
    runtime.close()

    # Rebuilt with *no* policy argument: the definition still carries it.
    rebuilt = extension_runtime(tmp_path)
    definition = rebuilt.process_store.get_definition("analyze_capability_gap", "1")
    stored = ExtensionPolicy.from_dict(definition.metadata["extension_policy"])
    assert not stored.allows(ExtensionStrategy.CODE_EXTENSION)
    rebuilt.close()
