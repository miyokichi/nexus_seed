"""AT8 (spec §64, §24): several capable processes, one reproducible choice.

Determinism is the requirement, not cleverness.  If selection varied between
runs — or between machines, or across a restart — the same work would behave
differently for no visible reason, and the audit record would explain nothing.
"""

from __future__ import annotations

from capability_helpers import register_capable

from nexus_seed.runtime.runtime import Runtime


def register_three(runtime):
    register_capable(runtime, "alpha", ("analyze_resistance",))
    register_capable(runtime, "beta", ("analyze_resistance",))
    register_capable(runtime, "gamma", ("analyze_resistance",))


def test_priority_decides_between_equals(tmp_path):
    """Spec §25: a definition bids for selection with a number."""
    runtime = Runtime(tmp_path / "m.db")
    register_capable(runtime, "plain", ("analyze_resistance",))
    register_capable(runtime, "preferred", ("analyze_resistance",), priority=50)

    result = runtime.match_capabilities(["analyze_resistance"])

    assert result.selected.definition_name == "preferred"
    assert result.selected.score > 100
    runtime.close()


def test_selection_is_stable_without_any_tie_breaker(tmp_path):
    """No priorities at all: still the same answer every time."""
    runtime = Runtime(tmp_path / "m.db")
    register_three(runtime)

    chosen = {
        runtime.match_capabilities(["analyze_resistance"]).selected.definition_name
        for _ in range(10)
    }
    assert len(chosen) == 1
    runtime.close()


def test_selection_is_stable_across_a_restart(tmp_path):
    """AT8: the registry is on disk, so the ranking is too."""
    db_path = tmp_path / "m.db"

    runtime = Runtime(db_path)
    register_three(runtime)
    first = runtime.match_capabilities(["analyze_resistance"]).selected.definition_name
    runtime.close()

    runtime2 = Runtime(db_path)
    second = runtime2.match_capabilities(["analyze_resistance"]).selected.definition_name
    assert second == first
    runtime2.close()


def test_selection_does_not_depend_on_registration_order(tmp_path):
    """Two systems that registered the same processes agree."""
    forward = Runtime(tmp_path / "a.db")
    for name in ("alpha", "beta", "gamma"):
        register_capable(forward, name, ("analyze_resistance",))
    chosen_forward = forward.match_capabilities(["analyze_resistance"]).selected
    forward.close()

    backward = Runtime(tmp_path / "b.db")
    for name in ("gamma", "beta", "alpha"):
        register_capable(backward, name, ("analyze_resistance",))
    chosen_backward = backward.match_capabilities(["analyze_resistance"]).selected
    backward.close()

    assert chosen_forward.definition_name == chosen_backward.definition_name


def test_a_newer_definition_version_wins_a_tie(tmp_path):
    from nexus_seed.capabilities.models import CapabilityRef
    from nexus_seed.core.process import ProcessDefinition
    from capability_helpers import worker

    runtime = Runtime(tmp_path / "m.db")
    for version in ("1", "2"):
        runtime.register_process(
            ProcessDefinition(
                name="analyzer",
                version=version,
                handler=f"analyzer_v{version}",
                provides_capabilities=(CapabilityRef("analyze_resistance"),),
            ),
            worker,
        )

    result = runtime.match_capabilities(["analyze_resistance"])
    assert result.selected.definition_version == "2"
    runtime.close()


def test_priority_beats_a_newer_version(tmp_path):
    """The explicit bid outranks the incidental tie-breaker."""
    from nexus_seed.capabilities.models import CapabilityRef
    from nexus_seed.core.process import ProcessDefinition
    from capability_helpers import worker

    runtime = Runtime(tmp_path / "m.db")
    runtime.register_process(
        ProcessDefinition(
            name="analyzer",
            version="1",
            handler="a1",
            metadata={"capability_priority": 10},
            provides_capabilities=(CapabilityRef("analyze_resistance"),),
        ),
        worker,
    )
    runtime.register_process(
        ProcessDefinition(
            name="analyzer",
            version="2",
            handler="a2",
            provides_capabilities=(CapabilityRef("analyze_resistance"),),
        ),
        worker,
    )

    assert runtime.match_capabilities(["analyze_resistance"]).selected.definition_version == "1"
    runtime.close()


def test_every_candidate_is_reported_not_just_the_winner(tmp_path):
    """The audit needs to show what was considered and rejected."""
    runtime = Runtime(tmp_path / "m.db")
    register_three(runtime)
    register_capable(runtime, "unrelated", ("something_else",))

    result = runtime.match_capabilities(["analyze_resistance"])

    # Only definitions that provide *something* asked for are candidates; a
    # process with nothing to do with this work is not "rejected", it was
    # never in the running.
    considered = {c.definition_name for c in result.candidates}
    assert considered == {"alpha", "beta", "gamma"}
    assert sum(1 for c in result.candidates if c.eligible) == 3
    assert "3 eligible candidate(s)" in result.reasons[0]
    runtime.close()


def test_an_ineligible_candidate_never_outranks_an_eligible_one(tmp_path):
    """Priority cannot buy eligibility."""
    runtime = Runtime(tmp_path / "m.db")
    register_capable(runtime, "loud_but_useless", ("a",), priority=999)
    register_capable(runtime, "quiet_and_capable", ("a", "b"))

    result = runtime.match_capabilities(["a", "b"])

    assert result.selected.definition_name == "quiet_and_capable"
    runtime.close()
