"""AT10–AT12, AT21 (spec §102–§105, §113): nothing proceeds because it was said.

The failures this guards against are particular to *self*-extension.  A model
choosing between plans can at worst name a plan that does not exist; a model
describing an extension can name a strategy nobody implements, quietly widen
what is being acquired, or ask for a permission that would let the extension
change the rules judging it.  Each of those is refused by name, and none of them
is a matter of degree.
"""

from __future__ import annotations

import uuid

from extension_helpers import needs

from nexus_seed.extension.models import (
    AcquisitionCandidate,
    AcquisitionFeasibility,
    CapabilityGap,
    CapabilityGapStatus,
    ComponentType,
    ExtensionProposal,
    ExtensionStrategy,
    ProposedComponent,
)
from nexus_seed.extension.policy import ExtensionPolicy
from nexus_seed.extension.strategies import implied_permissions
from nexus_seed.extension.validator import ExtensionValidator


def a_gap(*names) -> CapabilityGap:
    return CapabilityGap(
        work_requirement_id=uuid.uuid4(),
        missing_capabilities=[needs(n) for n in names],
    )


def a_candidate(strategy=ExtensionStrategy.ADD_EXTRACTOR, targets=("parse_powerpoint",)):
    return AcquisitionCandidate(
        strategy=strategy,
        target_capabilities=list(targets),
        feasibility=AcquisitionFeasibility.FEASIBLE,
    )


def an_extractor(name="ppt_extractor", provides=("parse_powerpoint",), **kwargs):
    return ProposedComponent(
        component_type=ComponentType.RESOURCE_EXTRACTOR.value,
        name=name,
        provides_capabilities=list(provides),
        **kwargs,
    )


def a_proposal(gap, *, strategy=ExtensionStrategy.ADD_EXTRACTOR, components=None, **kwargs):
    components = [an_extractor()] if components is None else components
    kwargs.setdefault("target_capabilities", list(gap.missing_capabilities))
    kwargs.setdefault(
        "required_permissions", implied_permissions(strategy, components)
    )
    return ExtensionProposal(
        capability_gap_id=gap.id,
        strategy=strategy,
        proposed_components=components,
        **kwargs,
    )


def validate(proposal, gap, *, candidates=None, policy=None, definitions=()):
    validator = ExtensionValidator()
    return validator.validate(
        proposal,
        gap=gap,
        candidates=candidates if candidates is not None else [a_candidate()],
        policy=policy,
        definitions=definitions,
    )


# --- the baseline -----------------------------------------------------------


def test_a_derived_proposal_validates():
    gap = a_gap("parse_powerpoint")
    result = validate(a_proposal(gap), gap)
    assert result.ok, result.reasons


# --- AT10: unknown strategy -------------------------------------------------


def test_an_unknown_strategy_is_invalid():
    gap = a_gap("parse_powerpoint")
    proposal = a_proposal(gap)
    proposal.strategy = None
    proposal.declared_strategy = "DELETE_RUNTIME_AND_REBUILD"

    result = validate(proposal, gap)

    assert not result.ok
    assert any("not a known extension strategy" in r for r in result.reasons)


def test_unsupported_is_not_a_strategy_a_proposal_may_take():
    gap = a_gap("parse_powerpoint")
    proposal = a_proposal(gap, strategy=ExtensionStrategy.UNSUPPORTED, components=[])

    result = validate(
        proposal, gap, candidates=[a_candidate(ExtensionStrategy.UNSUPPORTED)]
    )

    assert not result.ok


# --- AT11: outside the analyzed candidates ----------------------------------


def test_a_strategy_the_analyzer_did_not_find_is_invalid():
    """A route nothing here can carry out is not a bolder option (spec §103)."""
    gap = a_gap("parse_powerpoint")
    proposal = a_proposal(
        gap,
        strategy=ExtensionStrategy.ADD_EXTERNAL_PLUGIN,
        components=[
            ProposedComponent(
                component_type=ComponentType.PLUGIN.value,
                name="ppt-tools",
                provides_capabilities=["parse_powerpoint"],
            )
        ],
    )

    result = validate(proposal, gap, candidates=[a_candidate()])

    assert not result.ok
    assert any("not among the analyzed candidates" in r for r in result.reasons)


def test_a_strategy_the_policy_forbids_is_invalid():
    gap = a_gap("parse_powerpoint")
    policy = ExtensionPolicy(
        allowed_strategies=(ExtensionStrategy.REGISTER_EXISTING_PROCESS,)
    )

    result = validate(a_proposal(gap), gap, policy=policy)

    assert not result.ok
    assert any("not permitted by policy" in r for r in result.reasons)


# --- AT12: targets outside the gap ------------------------------------------


def test_a_target_outside_the_gap_is_invalid():
    gap = a_gap("parse_powerpoint")
    proposal = a_proposal(gap, target_capabilities=[needs("read_email")])

    result = validate(proposal, gap)

    assert not result.ok
    assert any("not missing in this gap" in r for r in result.reasons)


def test_a_component_claiming_extra_capabilities_is_invalid():
    """Quietly widening what is acquired is the interesting failure (spec §74)."""
    gap = a_gap("parse_powerpoint")
    proposal = a_proposal(
        gap, components=[an_extractor(provides=("parse_powerpoint", "send_email"))]
    )

    result = validate(proposal, gap)

    assert not result.ok
    assert any("outside this gap" in r for r in result.reasons)


def test_a_proposal_with_no_target_is_invalid():
    gap = a_gap("parse_powerpoint")
    proposal = a_proposal(gap, target_capabilities=[])
    result = validate(proposal, gap)
    assert not result.ok


# --- AT21: unknown component type -------------------------------------------


def test_an_unknown_component_type_is_invalid():
    gap = a_gap("parse_powerpoint")
    proposal = a_proposal(
        gap,
        components=[
            ProposedComponent(
                component_type="QUANTUM_ACCELERATOR",
                name="qa",
                provides_capabilities=["parse_powerpoint"],
            )
        ],
    )

    result = validate(proposal, gap)

    assert not result.ok
    assert any("unknown component_type" in r for r in result.reasons)


# --- structural checks ------------------------------------------------------


def test_a_component_may_not_require_what_it_provides():
    gap = a_gap("parse_powerpoint")
    proposal = a_proposal(
        gap,
        components=[
            an_extractor(requires_capabilities=["parse_powerpoint"]),
        ],
    )

    result = validate(proposal, gap)

    assert not result.ok
    assert any("itself meant to provide" in r for r in result.reasons)


def test_a_component_may_not_require_the_missing_capability():
    """A circular acquisition: buildable only once it is built."""
    gap = a_gap("parse_powerpoint", "render_slides")
    proposal = a_proposal(
        gap,
        components=[
            an_extractor(provides=("parse_powerpoint", "render_slides")),
            ProposedComponent(
                component_type=ComponentType.PROCESS_DEFINITION.value,
                name="renderer",
                requires_capabilities=["render_slides"],
            ),
        ],
    )

    result = validate(proposal, gap)

    assert not result.ok
    assert any("which this gap is missing" in r for r in result.reasons)


def test_a_proposal_that_would_not_close_the_gap_is_invalid():
    gap = a_gap("parse_powerpoint")
    proposal = a_proposal(gap, components=[an_extractor(provides=())])

    result = validate(proposal, gap)

    assert not result.ok
    assert any("no proposed component would provide" in r for r in result.reasons)


def test_a_reuse_proposal_must_name_something_to_reuse():
    gap = a_gap("generate_report")
    proposal = a_proposal(
        gap, strategy=ExtensionStrategy.REGISTER_EXISTING_PROCESS, components=[]
    )

    result = validate(
        proposal,
        gap,
        candidates=[a_candidate(ExtensionStrategy.REGISTER_EXISTING_PROCESS)],
    )

    assert not result.ok
    assert any("nothing to reuse and nothing to add" in r for r in result.reasons)


def test_a_resolved_gap_cannot_be_extended():
    gap = a_gap("parse_powerpoint")
    gap.status = CapabilityGapStatus.RESOLVED
    result = validate(a_proposal(gap), gap)
    assert not result.ok


def test_a_proposal_about_another_gap_is_invalid():
    gap = a_gap("parse_powerpoint")
    other = a_gap("parse_powerpoint")
    result = validate(a_proposal(other), gap)
    assert not result.ok


def test_a_missing_gap_is_invalid():
    gap = a_gap("parse_powerpoint")
    result = validate(a_proposal(gap), None)
    assert not result.ok


# --- permissions ------------------------------------------------------------


def test_under_declared_permissions_are_invalid():
    """The Phase 3C rule: saying less must not measure as less (spec §17)."""
    gap = a_gap("parse_powerpoint")
    proposal = a_proposal(gap, required_permissions=[])

    result = validate(proposal, gap)

    assert not result.ok
    assert any("does not declare the permission" in r for r in result.reasons)
    assert "process.register" in result.implied_permissions


def test_asking_to_change_the_rules_is_flagged_not_ignored():
    """spec §38/§79: recognised, recorded, and never decided automatically."""
    gap = a_gap("parse_powerpoint")
    proposal = a_proposal(
        gap,
        required_permissions=[
            *implied_permissions(ExtensionStrategy.ADD_EXTRACTOR, [an_extractor()]),
            "permission.modify",
        ],
    )

    result = validate(proposal, gap)

    assert result.ok  # it is a coherent proposal…
    assert result.requests_core_change  # …and it is not one to wave through
    assert any("change the rules it is judged by" in r for r in result.reasons)


def test_a_declared_core_change_is_flagged():
    gap = a_gap("add_agent_primitive")
    component = ProposedComponent(
        component_type=ComponentType.CODE_MODULE.value,
        name="agent",
        provides_capabilities=["add_agent_primitive"],
        metadata={"modifies_core": True},
    )
    proposal = a_proposal(
        gap, strategy=ExtensionStrategy.CODE_EXTENSION, components=[component]
    )

    result = validate(
        proposal, gap, candidates=[a_candidate(ExtensionStrategy.CODE_EXTENSION,
                                               targets=("add_agent_primitive",))]
    )

    assert result.requests_core_change


# --- already available ------------------------------------------------------


def test_a_capability_that_became_available_makes_the_proposal_invalid(tmp_path):
    """No extension is needed for something we can already do (spec §37)."""
    from extension_helpers import extension_runtime, register_capable

    runtime = extension_runtime(tmp_path)
    register_capable(runtime, "reporter", ("generate_report",))
    gap = a_gap("generate_report")
    proposal = a_proposal(
        gap,
        strategy=ExtensionStrategy.CODE_EXTENSION,
        components=[
            ProposedComponent(
                component_type=ComponentType.CODE_MODULE.value,
                name="reporter_module",
                provides_capabilities=["generate_report"],
            )
        ],
    )

    result = runtime.extension_validator.validate(
        proposal,
        gap=gap,
        candidates=[a_candidate(ExtensionStrategy.CODE_EXTENSION, ("generate_report",))],
        definitions=runtime.process_store.all_definitions(),
    )

    assert not result.ok
    assert any("already provided" in r for r in result.reasons)
    runtime.close()


def test_a_disabled_provider_does_not_count_as_already_available(tmp_path):
    """Otherwise the best route this phase has would be refused (spec §71)."""
    from extension_helpers import extension_runtime, register_disabled_provider

    runtime = extension_runtime(tmp_path)
    register_disabled_provider(runtime, "reporter", "generate_report")
    gap = a_gap("generate_report")
    proposal = ExtensionProposal(
        capability_gap_id=gap.id,
        strategy=ExtensionStrategy.REGISTER_EXISTING_PROCESS,
        target_capabilities=list(gap.missing_capabilities),
        reusable_components=["reporter:1"],
        required_permissions=["capability.enable"],
    )

    result = runtime.extension_validator.validate(
        proposal,
        gap=gap,
        candidates=[
            a_candidate(
                ExtensionStrategy.REGISTER_EXISTING_PROCESS, ("generate_report",)
            )
        ],
        definitions=runtime.process_store.all_definitions(),
    )

    assert result.ok, result.reasons
    runtime.close()
