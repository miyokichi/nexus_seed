"""AT6, AT7, AT8 (spec §98–§100): working out what could close a gap.

The analyzer is deterministic on purpose (spec §25).  Asked "how could I acquire
this?" with nothing in front of it, a language model answers "write some code"
almost every time — because writing code is the answer that always applies.
Discovering that a small extractor slot already exists, or that the mechanical
half is already registered, requires looking, and looking is what this does.
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    CapabilityGap,
    block,
    candidate_strategies,
    candidates_for,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
    reopen,
)


def unmapped_gap(names, hints=None):
    """A gap for capabilities nothing in the architecture speaks to."""
    import uuid

    return CapabilityGap(
        work_requirement_id=uuid.uuid4(),
        missing_capabilities=[needs(n, hints) for n in names],
    )


async def test_a_missing_format_becomes_an_extractor(tmp_path):
    """AT6: the resource layer exists, the extractor does not (spec §26)."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)

    gap = only_gap(runtime)
    candidates = candidates_for(runtime, gap)

    assert [c.strategy.value for c in candidates] == ["ADD_EXTRACTOR"]
    top = candidates[0]
    assert top.feasibility.value == "FEASIBLE"
    assert top.estimated_risk.value == "MEDIUM"
    assert "resources:ExtractorRegistry" in top.reusable_components
    assert [c.component_type for c in top.required_new_components] == [
        "RESOURCE_EXTRACTOR"
    ]
    runtime.close()


async def test_a_format_we_can_already_read_needs_only_a_process(tmp_path):
    """An existing extractor changes the answer from "add one" to "use it"."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(
        runtime,
        required=[
            needs("parse_csv_report", {"resource_type": "csv", "representation": "structure"})
        ],
    )
    await block(runtime, requirement)

    assert candidate_strategies(runtime, only_gap(runtime)) == ["ADD_PROCESS_DEFINITION"]
    top = candidates_for(runtime, only_gap(runtime))[0]
    assert "extractor:structure/csv" in top.reusable_components
    runtime.close()


async def test_new_code_is_the_last_resort(tmp_path):
    """AT7: nothing to reuse, nothing declared — and it says so (spec §73)."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_unknown_binary_format")])
    await block(runtime, requirement)

    gap = only_gap(runtime)
    candidates = candidates_for(runtime, gap)

    assert [c.strategy.value for c in candidates] == ["CODE_EXTENSION"]
    top = candidates[0]
    assert top.estimated_risk.value == "HIGH"
    # Not claimed as feasible: nobody has established that this is buildable.
    assert top.feasibility.value == "UNKNOWN"
    assert top.reusable_components == []
    assert "repository.modify" in top.required_permissions
    runtime.close()


async def test_candidate_order_is_stable_across_restarts(tmp_path):
    """AT8: the same registry and the same gap give the same order (spec §100)."""
    runtime = extension_runtime(tmp_path)
    from extension_helpers import register_configurable, register_disabled_provider

    register_disabled_provider(runtime, "slides", "parse_powerpoint")
    register_configurable(runtime, "flexible", "parse_powerpoint")
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)

    gap_id = only_gap(runtime).id
    first = candidate_strategies(runtime, runtime.get_capability_gap(gap_id))
    again = candidate_strategies(runtime, runtime.get_capability_gap(gap_id))
    runtime.close()

    rebuilt = reopen(tmp_path)
    from extension_helpers import register_configurable as reconfigure
    from extension_helpers import register_disabled_provider as redisable

    redisable(rebuilt, "slides", "parse_powerpoint")
    reconfigure(rebuilt, "flexible", "parse_powerpoint")
    after_restart = candidate_strategies(rebuilt, rebuilt.get_capability_gap(gap_id))

    assert first == again == after_restart
    # And the order is reuse-first, not registration order (Invariant 88).
    assert first[0] == "REGISTER_EXISTING_PROCESS"
    assert first.index("CONFIGURE_EXISTING_PROCESS") < first.index("ADD_EXTRACTOR")
    rebuilt.close()


async def test_two_missing_capabilities_merge_into_one_route(tmp_path):
    """A candidate is a way to proceed, not a matrix of capability × strategy."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(
        runtime,
        required=[
            needs("parse_powerpoint", POWERPOINT),
            needs("parse_keynote", {"resource_type": "keynote"}),
        ],
    )
    await block(runtime, requirement)

    candidates = candidates_for(runtime, only_gap(runtime))

    assert [c.strategy.value for c in candidates] == ["ADD_EXTRACTOR"]
    merged = candidates[0]
    assert sorted(merged.target_capabilities) == ["parse_keynote", "parse_powerpoint"]
    assert len(merged.required_new_components) == 2
    runtime.close()


async def test_an_unserviceable_capability_is_not_dressed_up(tmp_path):
    """AT: no route exists, and the analyzer declines to invent one (spec §65)."""
    runtime = extension_runtime(tmp_path)
    gap = unmapped_gap(["become_conscious"], {"unsupported": True})

    candidates = candidates_for(runtime, gap)

    assert [c.strategy.value for c in candidates] == ["UNSUPPORTED"]
    assert candidates[0].feasibility.value == "UNSUPPORTED"
    assert candidates[0].estimated_risk.value == "CRITICAL"
    runtime.close()


async def test_a_core_change_is_never_an_ordinary_route(tmp_path):
    """"Add a seventh primitive" is recognised, not planned (spec §38)."""
    runtime = extension_runtime(tmp_path)
    gap = unmapped_gap(["add_agent_primitive"], {"requires_core_change": True})

    candidates = candidates_for(runtime, gap)

    assert [c.strategy.value for c in candidates] == ["UNSUPPORTED"]
    runtime.close()


async def test_an_unserviceable_gap_produces_no_proposal(tmp_path):
    """The gap is kept and the system says why (spec §66)."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(
        runtime, required=[needs("become_conscious", {"unsupported": True})]
    )
    events = await block(runtime, requirement), runtime.event_store.all()

    assert runtime.get_extension_proposals() == []
    gap = only_gap(runtime)
    assert gap.status.value == "OPEN"
    assert not gap.status.terminal
    types = [e.type for e in events[1]]
    assert "capability_acquisition_unavailable" in types
    runtime.close()


async def test_the_proposal_records_the_routes_it_chose_between(tmp_path):
    """The candidate set is kept on the proposal — the boundary stays visible."""
    runtime = extension_runtime(tmp_path)
    from extension_helpers import register_disabled_provider

    register_disabled_provider(runtime, "slides", "parse_powerpoint")
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)

    proposal = only_proposal(runtime)

    assert proposal.declared_strategy == "REGISTER_EXISTING_PROCESS"
    assert "ADD_EXTRACTOR" in proposal.candidate_strategies
    assert [c["strategy"] for c in proposal.analysis["candidates"]] == (
        proposal.candidate_strategies
    )
    runtime.close()
