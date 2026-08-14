"""AT4 (spec §96): reuse before construction, when reuse actually exists.

This is the invariant the whole phase rests on (Invariant 88).  A system that
can propose writing new code will always find that easier than discovering what
it already has — the reuse route requires looking at itself, and the code route
requires only the willingness to write code.  So the ordering is arithmetic
here, not advice: :data:`STRATEGY_ORDER` decides, and the tests below check that
the *decision* comes out reuse-first even when the reusable thing is invisible
to ordinary matching.
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    ExtensionRisk,
    block,
    candidates_for,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
    register_capable,
    register_configurable,
    register_disabled_provider,
)


async def test_a_disabled_provider_is_found_and_preferred(tmp_path):
    """AT4 (spec §71): the competence is written already; it is switched off."""
    runtime = extension_runtime(tmp_path)
    register_disabled_provider(runtime, "report_generator", "generate_report")
    requirement = gap_work(runtime, required=[needs("generate_report", POWERPOINT)])

    await block(runtime, requirement)

    proposal = only_proposal(runtime)
    assert proposal.declared_strategy == "REGISTER_EXISTING_PROCESS"
    assert "report_generator:1" in proposal.reusable_components
    # Reuse adds nothing, and its risk says so.
    assert proposal.proposed_components == []
    assert proposal.estimated_risk is ExtensionRisk.LOW
    runtime.close()


async def test_a_disabled_process_counts_as_reusable_too(tmp_path):
    """The capability is enabled; the *process* providing it is not."""
    runtime = extension_runtime(tmp_path)
    register_capable(runtime, "report_generator", ("generate_report",), enabled=False)
    requirement = gap_work(runtime, required=[needs("generate_report")])

    await block(runtime, requirement)

    proposal = only_proposal(runtime)
    assert proposal.declared_strategy == "REGISTER_EXISTING_PROCESS"
    assert "report_generator:1" in proposal.reusable_components
    assert "disabled" in " ".join(
        c["reasons"][0] for c in proposal.analysis["candidates"] if c["reasons"]
    )
    runtime.close()


async def test_a_configurable_process_beats_new_code(tmp_path):
    """Step 2 of the ordering: an existing process under another setting."""
    runtime = extension_runtime(tmp_path)
    register_configurable(runtime, "flexible_reader", "parse_powerpoint")
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])

    await block(runtime, requirement)

    proposal = only_proposal(runtime)
    assert proposal.declared_strategy == "CONFIGURE_EXISTING_PROCESS"
    assert "flexible_reader:1" in proposal.reusable_components
    assert proposal.estimated_risk is ExtensionRisk.LOW
    # The extractor route was found too — it was simply not preferred.
    assert "ADD_EXTRACTOR" in proposal.candidate_strategies
    runtime.close()


async def test_reuse_outranks_construction_even_when_both_exist(tmp_path):
    """AT73: with a reuse route available, new code is never the top candidate."""
    runtime = extension_runtime(tmp_path)
    register_disabled_provider(runtime, "slides", "parse_powerpoint")
    register_configurable(runtime, "flexible", "parse_powerpoint")
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)

    order = [c.strategy.value for c in candidates_for(runtime, only_gap(runtime))]

    assert order[0] == "REGISTER_EXISTING_PROCESS"
    assert order.index("CONFIGURE_EXISTING_PROCESS") < order.index("ADD_EXTRACTOR")
    assert "CODE_EXTENSION" not in order  # a route existed, so none was invented
    runtime.close()


async def test_an_enabled_provider_is_not_a_gap_at_all(tmp_path):
    """Nothing is proposed for work the system can simply do."""
    runtime = extension_runtime(tmp_path)
    register_capable(runtime, "report_generator", ("generate_report",))
    requirement = gap_work(runtime, required=[needs("generate_report")])

    result = await block(runtime, requirement)

    assert result.status.value != "BLOCKED_CAPABILITY"
    assert runtime.get_capability_gaps() == []
    assert runtime.get_extension_proposals() == []
    runtime.close()


async def test_the_reuse_proposal_asks_for_the_smallest_permission(tmp_path):
    """Re-enabling something is not the same power as writing code (spec §76)."""
    runtime = extension_runtime(tmp_path)
    register_disabled_provider(runtime, "report_generator", "generate_report")
    requirement = gap_work(runtime, required=[needs("generate_report")])

    await block(runtime, requirement)

    proposal = only_proposal(runtime)
    assert proposal.required_permissions == ["capability.enable"]
    assert "repository.modify" not in proposal.required_permissions
    runtime.close()
