"""AT10, AT11, AT21 through the model path (spec §102, §103, §113).

The same three refusals as the unit tests, but reached the way they would be in
practice: a model answers, and the answer is wrong in a way that would matter.
What must hold at the end of each is identical — the proposal is recorded as
INVALID, and nothing about the system changed (Invariant 87).
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    ExtensionProposalStatus,
    ExtensionRisk,
    block,
    elaborates,
    extension_runtime,
    gap_work,
    install_extension_llm,
    needs,
    only_gap,
    only_proposal,
    proposal_response,
)


def ppt_work(runtime):
    return gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])


def nothing_changed(runtime):
    """Phase 5A's boundary, checked after every refusal (Invariant 90)."""
    assert runtime.get_capability("parse_powerpoint") is None
    assert runtime.process_store.get_definition("powerpoint_extractor", "1") is None
    assert runtime.extractors.find("structure", "powerpoint") is None


async def test_an_unknown_strategy_is_recorded_invalid(tmp_path):
    """AT10: "DELETE_RUNTIME_AND_REBUILD" is refused by name (spec §102)."""
    runtime = extension_runtime(tmp_path)
    install_extension_llm(
        runtime,
        elaborates("DELETE_RUNTIME_AND_REBUILD", capability="parse_powerpoint"),
    )

    await block(runtime, ppt_work(runtime))

    proposal = only_proposal(runtime)
    assert proposal.status is ExtensionProposalStatus.INVALID
    assert proposal.strategy is None
    assert proposal.declared_strategy == "DELETE_RUNTIME_AND_REBUILD"
    assert any("not a known extension strategy" in r for r in proposal.reasons)
    # Unknown is never treated as safe (spec §41).
    assert proposal.estimated_risk is ExtensionRisk.CRITICAL
    nothing_changed(runtime)
    runtime.close()


async def test_a_strategy_outside_the_candidates_is_recorded_invalid(tmp_path):
    """AT11: a well-formed answer about a system that does not exist (§103)."""
    runtime = extension_runtime(tmp_path)
    install_extension_llm(
        runtime,
        elaborates(
            "ADD_EXTERNAL_PLUGIN",
            capability="parse_powerpoint",
            component_type="PLUGIN",
            component="ppt-tools",
            permissions=("plugin.install", "network.access"),
            risk="HIGH",
        ),
    )

    await block(runtime, ppt_work(runtime))

    proposal = only_proposal(runtime)
    assert proposal.status is ExtensionProposalStatus.INVALID
    assert any("not among the analyzed candidates" in r for r in proposal.reasons)
    assert proposal.candidate_strategies == ["ADD_EXTRACTOR"]
    nothing_changed(runtime)
    runtime.close()


async def test_a_hallucinated_component_type_is_recorded_invalid(tmp_path):
    """AT21: nothing knows how to build a QUANTUM_ACCELERATOR (spec §113)."""
    runtime = extension_runtime(tmp_path)
    install_extension_llm(
        runtime,
        elaborates(
            "ADD_EXTRACTOR",
            capability="parse_powerpoint",
            component_type="QUANTUM_ACCELERATOR",
            component="qa",
        ),
    )

    await block(runtime, ppt_work(runtime))

    proposal = only_proposal(runtime)
    assert proposal.status is ExtensionProposalStatus.INVALID
    assert any("unknown component_type" in r for r in proposal.reasons)
    assert proposal.estimated_risk is ExtensionRisk.CRITICAL
    nothing_changed(runtime)
    runtime.close()


async def test_a_widened_target_is_recorded_invalid(tmp_path):
    """AT12 through the model: acquiring more than the gap asked for (§74)."""
    runtime = extension_runtime(tmp_path)
    install_extension_llm(
        runtime,
        proposal_response(
            {
                "strategy": "ADD_EXTRACTOR",
                "title": "A rather ambitious extractor",
                "description": "reads slides, and also sends mail",
                "proposed_components": [
                    {
                        "component_type": "RESOURCE_EXTRACTOR",
                        "name": "ppt_extractor",
                        "provides_capabilities": ["parse_powerpoint", "send_email"],
                        "required_permissions": [],
                    }
                ],
                "required_permissions": ["repository.read", "process.register"],
                "estimated_risk": "MEDIUM",
                "rationale": "while we are in there",
            }
        ),
    )

    await block(runtime, ppt_work(runtime))

    proposal = only_proposal(runtime)
    assert proposal.status is ExtensionProposalStatus.INVALID
    assert any("outside this gap" in r for r in proposal.reasons)
    nothing_changed(runtime)
    runtime.close()


async def test_an_under_declared_permission_is_recorded_invalid(tmp_path):
    """Saying less about yourself must not measure as less (spec §17)."""
    runtime = extension_runtime(tmp_path)
    install_extension_llm(
        runtime,
        elaborates("ADD_EXTRACTOR", capability="parse_powerpoint", permissions=()),
    )

    await block(runtime, ppt_work(runtime))

    proposal = only_proposal(runtime)
    assert proposal.status is ExtensionProposalStatus.INVALID
    assert any("does not declare the permission" in r for r in proposal.reasons)
    runtime.close()


async def test_an_invalid_proposal_leaves_the_gap_open(tmp_path):
    """The deficiency is real whatever the model said about it (spec §66)."""
    runtime = extension_runtime(tmp_path)
    install_extension_llm(
        runtime, elaborates("DELETE_RUNTIME_AND_REBUILD", capability="parse_powerpoint")
    )

    await block(runtime, ppt_work(runtime))

    gap = only_gap(runtime)
    assert gap.status.value == "OPEN"
    assert not gap.status.terminal
    assert gap.missing_names == ["parse_powerpoint"]
    runtime.close()


async def test_the_refusal_is_recorded_as_a_decision(tmp_path):
    """A rejection is part of the decision history, not an absence of one."""
    runtime = extension_runtime(tmp_path)
    install_extension_llm(
        runtime, elaborates("DELETE_RUNTIME_AND_REBUILD", capability="parse_powerpoint")
    )

    await block(runtime, ppt_work(runtime))
    proposal = only_proposal(runtime)
    decisions = runtime.get_extension_decisions(proposal.id)

    assert len(decisions) == 1
    assert decisions[0].decision.value == "REJECT"
    assert not decisions[0].validation_ok
    assert decisions[0].granted_permissions == []
    types = [e.type for e in runtime.event_store.all()]
    assert "extension_proposal_invalid" in types
    assert "extension_approved" not in types
    runtime.close()
