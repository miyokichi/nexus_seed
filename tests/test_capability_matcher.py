"""AT3, AT6, AT7 (spec §59, §62, §63): one process, or a recorded gap.

Phase 4A answers with a single process or nothing (Invariant 53).  The three
outcomes are kept distinct because they call for different responses: go,
*we cannot do this at all*, and *we cannot do it in one step*.
"""

from __future__ import annotations

from capability_helpers import register_capable, requirements

from nexus_seed.capabilities.models import CapabilityMatchStatus
from nexus_seed.runtime.runtime import Runtime


def matched(runtime, *names):
    return runtime.match_capabilities(names)


def test_one_capable_process_is_selected(tmp_path):
    """AT3."""
    runtime = Runtime(tmp_path / "m.db")
    register_capable(runtime, "resistance_analyzer", ("analyze_resistance",))

    result = matched(runtime, "analyze_resistance")

    assert result.status is CapabilityMatchStatus.MATCHED_SINGLE_PROCESS
    assert result.eligible
    assert result.selected.key == ("resistance_analyzer", "1")
    assert result.selected.covered_capabilities == ["analyze_resistance"]
    assert result.selected.missing_capabilities == []
    runtime.close()


def test_a_process_covering_everything_is_eligible(tmp_path):
    """AT7."""
    runtime = Runtime(tmp_path / "m.db")
    register_capable(runtime, "combined", ("a", "b"))

    result = matched(runtime, "a", "b")

    assert result.eligible
    assert result.selected.key == ("combined", "1")
    assert sorted(result.selected.covered_capabilities) == ["a", "b"]
    runtime.close()


def test_partial_coverage_is_not_combined(tmp_path):
    """AT6: P1 has A, P2 has B — Phase 4A refuses to invent P1→P2."""
    runtime = Runtime(tmp_path / "m.db")
    register_capable(runtime, "p1", ("a",))
    register_capable(runtime, "p2", ("b",))

    result = matched(runtime, "a", "b")

    assert result.status is CapabilityMatchStatus.COMPOSITION_REQUIRED
    assert not result.eligible
    assert result.selected is None
    # Both were considered, and each says exactly what it lacked.
    by_name = {c.definition_name: c for c in result.candidates}
    assert by_name["p1"].missing_capabilities == ["b"]
    assert by_name["p2"].missing_capabilities == ["a"]
    assert "Phase 4B" in result.reasons[0]
    runtime.close()


def test_a_capability_nobody_provides_is_missing_not_composition(tmp_path):
    """The distinction that matters for self-extension later (spec §92)."""
    runtime = Runtime(tmp_path / "m.db")
    register_capable(runtime, "p1", ("a",))

    result = matched(runtime, "a", "summarize_semiconductor_report")

    assert result.status is CapabilityMatchStatus.MISSING_CAPABILITY
    assert result.missing_capabilities == ["summarize_semiconductor_report"]
    runtime.close()


def test_an_empty_system_reports_everything_missing(tmp_path):
    runtime = Runtime(tmp_path / "m.db")
    result = matched(runtime, "analyze_resistance")

    assert result.status is CapabilityMatchStatus.MISSING_CAPABILITY
    assert result.missing_capabilities == ["analyze_resistance"]
    assert result.candidates == []
    runtime.close()


def test_no_requirements_matches_anything_available(tmp_path):
    """Work that needs nothing is trivially doable by the first candidate."""
    runtime = Runtime(tmp_path / "m.db")
    register_capable(runtime, "p1", ("a",))

    result = runtime.match_capabilities([])

    assert result.eligible
    assert result.selected.covered_capabilities == []
    runtime.close()


def test_a_version_constraint_narrows_the_candidates(tmp_path):
    from nexus_seed.capabilities.models import CapabilityRef, CapabilityRequirement
    from nexus_seed.context.requirements import ContextRequirements
    from nexus_seed.core.process import ProcessDefinition
    from capability_helpers import worker

    runtime = Runtime(tmp_path / "m.db")
    runtime.register_process(
        ProcessDefinition(
            name="old",
            version="1",
            handler="old",
            provides_capabilities=(CapabilityRef("analyze_resistance", "1"),),
            context_requirements=ContextRequirements(),
        ),
        worker,
    )
    runtime.register_process(
        ProcessDefinition(
            name="new",
            version="1",
            handler="new",
            provides_capabilities=(CapabilityRef("analyze_resistance", "2"),),
            context_requirements=ContextRequirements(),
        ),
        worker,
    )

    result = runtime.match_capabilities(
        [CapabilityRequirement(name="analyze_resistance", version_constraint="2")]
    )
    assert result.selected.definition_name == "new"

    result_v1 = runtime.match_capabilities(
        [CapabilityRequirement(name="analyze_resistance", version_constraint="1")]
    )
    assert result_v1.selected.definition_name == "old"
    runtime.close()


def test_optional_requirements_do_not_decide_eligibility(tmp_path):
    """Spec §22: a nice-to-have can rank, never block."""
    runtime = Runtime(tmp_path / "m.db")
    register_capable(runtime, "plain", ("a",))
    register_capable(runtime, "richer", ("a", "b"))

    reqs = requirements("a") + requirements("b", required=False)
    result = runtime.capability_matcher.match(reqs, runtime.process_store.all_definitions())

    assert result.eligible
    # Both are eligible; the one that also covers the optional wins.
    assert {c.definition_name for c in result.candidates if c.eligible} == {
        "plain",
        "richer",
    }
    assert result.selected.definition_name == "richer"
    assert result.selected.optional_covered == ["b"]
    runtime.close()


def test_candidate_reasons_explain_each_verdict(tmp_path):
    runtime = Runtime(tmp_path / "m.db")
    register_capable(runtime, "p1", ("a",))
    register_capable(runtime, "p2", ("a", "b"))

    result = matched(runtime, "a", "b")
    by_name = {c.definition_name: c for c in result.candidates}

    assert "does not provide ['b']" in by_name["p1"].reasons[0]
    assert "provides all of ['a', 'b']" in by_name["p2"].reasons[0]
    assert "selected" in by_name["p2"].reasons[-1]
    runtime.close()


def test_matching_uses_no_llm(tmp_path):
    """Spec §26/§99: Phase 4A selection is exact, never inferred."""
    runtime = Runtime(tmp_path / "m.db")
    register_capable(runtime, "p1", ("analyze_resistance",))
    runtime.backends["llm"] = object()  # would explode if the matcher used it

    result = matched(runtime, "analyze_resistance")

    assert result.eligible
    assert not hasattr(runtime.capability_matcher, "backend")
    runtime.close()


def test_descriptions_and_tags_are_recorded_but_not_matched_on(tmp_path):
    """Spec §51, §53: stored for later, ignored now."""
    from nexus_seed.capabilities.models import Capability

    runtime = Runtime(tmp_path / "m.db")
    runtime.register_capability(
        Capability(
            name="analyze_resistance",
            description="looks a lot like summarising a report",
            tags=["summary", "report"],
        )
    )
    register_capable(runtime, "p1", ("analyze_resistance",))

    # A requirement whose *name* differs finds nothing, however similar the text.
    result = matched(runtime, "summarize_report")
    assert result.status is CapabilityMatchStatus.MISSING_CAPABILITY
    runtime.close()
