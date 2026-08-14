"""The extension domain models, tested where their rules actually live.

Three of these carry weight beyond their size:

* an unreadable strategy or risk level is never mapped onto a plausible one —
  unknown must reach validation as unknown (spec §35, §41);
* the fingerprint identifies a proposal's *content*, which is what makes
  redelivery idempotent (spec §68);
* APPROVED and RESOLVED are different words for different facts (spec §62).
"""

from __future__ import annotations

import uuid

from extension_helpers import needs

from nexus_seed.extension.models import (
    AcquisitionFeasibility,
    CapabilityGap,
    CapabilityGapStatus,
    ComponentType,
    ExtensionProposal,
    ExtensionProposalStatus,
    ExtensionRisk,
    ExtensionStrategy,
    ProposedComponent,
)
from nexus_seed.extension.strategies import (
    STRATEGY_ORDER,
    classify_risk,
    implied_permissions,
    strategy_rank,
)


def a_gap(*names, hints=None) -> CapabilityGap:
    return CapabilityGap(
        work_requirement_id=uuid.uuid4(),
        missing_capabilities=[needs(n, hints) for n in names],
    )


def a_proposal(gap, strategy=ExtensionStrategy.ADD_EXTRACTOR, **kwargs) -> ExtensionProposal:
    kwargs.setdefault("target_capabilities", list(gap.missing_capabilities))
    return ExtensionProposal(capability_gap_id=gap.id, strategy=strategy, **kwargs)


# --- strategies -------------------------------------------------------------


def test_an_unknown_strategy_stays_unknown():
    assert ExtensionStrategy.coerce("DELETE_RUNTIME_AND_REBUILD") is None
    assert ExtensionStrategy.coerce("add_extractor") is ExtensionStrategy.ADD_EXTRACTOR


def test_the_strategy_order_is_reuse_first():
    ranks = [strategy_rank(s) for s in STRATEGY_ORDER]
    assert ranks == sorted(ranks)
    assert strategy_rank(ExtensionStrategy.REGISTER_EXISTING_PROCESS) < strategy_rank(
        ExtensionStrategy.CODE_EXTENSION
    )
    assert strategy_rank(None) == len(STRATEGY_ORDER)


def test_every_strategy_is_ranked_and_classified():
    from nexus_seed.extension.strategies import (
        STRATEGY_COMPONENT_TYPE,
        STRATEGY_PERMISSIONS,
        STRATEGY_RISK,
    )

    for strategy in ExtensionStrategy:
        assert strategy in STRATEGY_ORDER
        assert strategy in STRATEGY_RISK
        assert strategy in STRATEGY_PERMISSIONS
        assert strategy in STRATEGY_COMPONENT_TYPE


# --- risk -------------------------------------------------------------------


def test_risk_rises_with_what_the_strategy_touches():
    low = classify_risk(ExtensionStrategy.REGISTER_EXISTING_PROCESS)
    medium = classify_risk(ExtensionStrategy.ADD_EXTRACTOR)
    high = classify_risk(ExtensionStrategy.CODE_EXTENSION)
    assert (low, medium, high) == (
        ExtensionRisk.LOW,
        ExtensionRisk.MEDIUM,
        ExtensionRisk.HIGH,
    )


def test_an_unknown_strategy_is_critical_not_low():
    """Unknown is never treated as safe (spec §41)."""
    assert classify_risk(None) is ExtensionRisk.CRITICAL
    assert classify_risk(ExtensionStrategy.UNSUPPORTED) is ExtensionRisk.CRITICAL


def test_an_unknown_component_type_is_critical():
    component = ProposedComponent(component_type="NEURAL_IMPLANT", name="x")
    assert (
        classify_risk(ExtensionStrategy.ADD_EXTRACTOR, components=[component])
        is ExtensionRisk.CRITICAL
    )


def test_a_permission_that_reaches_outside_raises_the_floor():
    """A MEDIUM strategy asking for repository access is not MEDIUM (spec §78)."""
    risk = classify_risk(
        ExtensionStrategy.ADD_EXTRACTOR, permissions=["repository.modify"]
    )
    assert risk is ExtensionRisk.HIGH


def test_changing_the_rules_is_always_critical():
    """runtime.modify / permission.modify are never ordinary (spec §79)."""
    for permission in ("runtime.modify", "permission.modify"):
        assert (
            classify_risk(ExtensionStrategy.ADD_EXTRACTOR, permissions=[permission])
            is ExtensionRisk.CRITICAL
        )


def test_a_declared_core_change_is_critical():
    component = ProposedComponent(
        component_type=ComponentType.CODE_MODULE.value,
        name="agent_primitive",
        metadata={"modifies_core": True},
    )
    assert (
        classify_risk(ExtensionStrategy.CODE_EXTENSION, components=[component])
        is ExtensionRisk.CRITICAL
    )


def test_an_unreadable_risk_level_is_kept_as_critical():
    """Unparseable risk must never mean "safe" (the Phase 3C rule)."""
    gap = a_gap("x")
    proposal = ExtensionProposal.from_output(
        {"strategy": "ADD_EXTRACTOR", "estimated_risk": "totally fine"},
        capability_gap_id=gap.id,
        work_requirement_id=None,
        target_capabilities=list(gap.missing_capabilities),
        candidate_strategies=["ADD_EXTRACTOR"],
    )
    assert proposal.estimated_risk is ExtensionRisk.CRITICAL


def test_implied_permissions_union_strategy_and_components():
    component = ProposedComponent(
        component_type=ComponentType.PLUGIN.value,
        name="p",
        required_permissions=["network.access"],
    )
    permissions = implied_permissions(ExtensionStrategy.ADD_EXTRACTOR, [component])
    assert "process.register" in permissions
    assert "network.access" in permissions


# --- proposals --------------------------------------------------------------


def test_the_fingerprint_identifies_content_not_identity():
    """Two proposals with the same content are the same proposal (spec §68)."""
    gap = a_gap("parse_powerpoint")
    first = a_proposal(gap)
    second = a_proposal(gap)
    assert first.id != second.id
    assert first.fingerprint == second.fingerprint


def test_the_fingerprint_separates_strategies():
    gap = a_gap("parse_powerpoint")
    extractor = a_proposal(gap, ExtensionStrategy.ADD_EXTRACTOR)
    code = a_proposal(gap, ExtensionStrategy.CODE_EXTENSION)
    assert extractor.fingerprint != code.fingerprint


def test_the_fingerprint_separates_gaps():
    first = a_proposal(a_gap("parse_powerpoint"))
    second = a_proposal(a_gap("parse_powerpoint"))
    assert first.fingerprint != second.fingerprint


def test_the_fingerprint_notices_a_different_component():
    gap = a_gap("parse_powerpoint")
    plain = a_proposal(gap)
    with_component = a_proposal(
        gap,
        proposed_components=[
            ProposedComponent(
                component_type=ComponentType.RESOURCE_EXTRACTOR.value, name="ppt"
            )
        ],
    )
    assert plain.fingerprint != with_component.fingerprint


def test_a_proposal_is_its_own_chain_root():
    proposal = a_proposal(a_gap("x"))
    assert proposal.root_proposal_id == proposal.id
    assert proposal.replaces_proposal_id is None


def test_unreadable_output_is_not_a_proposal():
    gap = a_gap("x")
    for data in (None, "text", 42, {}, {"title": "no strategy"}):
        assert (
            ExtensionProposal.from_output(
                data,
                capability_gap_id=gap.id,
                work_requirement_id=None,
                target_capabilities=[],
                candidate_strategies=[],
            )
            is None
        )


def test_an_unknown_strategy_survives_to_be_refused_by_name():
    """A readable answer that is wrong is kept, not retried away (spec §102)."""
    gap = a_gap("x")
    proposal = ExtensionProposal.from_output(
        {"strategy": "DELETE_RUNTIME_AND_REBUILD"},
        capability_gap_id=gap.id,
        work_requirement_id=None,
        target_capabilities=list(gap.missing_capabilities),
        candidate_strategies=["ADD_EXTRACTOR"],
    )
    assert proposal is not None
    assert proposal.strategy is None
    assert proposal.declared_strategy == "DELETE_RUNTIME_AND_REBUILD"
    assert "not a known strategy" in proposal.reasons[0]


# --- gaps -------------------------------------------------------------------


def test_approved_is_not_resolved():
    """The distinction the whole phase turns on (spec §62)."""
    assert not CapabilityGapStatus.PROPOSAL_APPROVED.terminal
    assert CapabilityGapStatus.RESOLVED.terminal
    assert CapabilityGapStatus.PROPOSAL_APPROVED is not CapabilityGapStatus.RESOLVED


def test_live_statuses_are_the_undecided_ones():
    live = [s for s in ExtensionProposalStatus if s.live]
    assert set(live) == {
        ExtensionProposalStatus.PENDING,
        ExtensionProposalStatus.VALIDATED,
        ExtensionProposalStatus.REVIEW,
    }


def test_feasibility_orders_from_certain_to_impossible():
    assert AcquisitionFeasibility.FEASIBLE.rank < AcquisitionFeasibility.UNKNOWN.rank
    assert AcquisitionFeasibility.UNKNOWN.rank < AcquisitionFeasibility.UNSUPPORTED.rank
