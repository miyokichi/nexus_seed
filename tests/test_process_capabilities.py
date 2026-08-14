"""ProcessDefinition → capability declaration (spec §7–§9, §82).

A process says what it can accomplish; the relation is persisted separately so
the registry can be queried from either end and survives a restart.
"""

from __future__ import annotations

from capability_helpers import capable_definition, register_capable, worker

from nexus_seed.capabilities.models import CapabilityRef
from nexus_seed.processes.actions import WRITE_ANALYSIS_RESULT
from nexus_seed.processes.work_intelligence import RESISTANCE_CHECK
from nexus_seed.runtime.runtime import Runtime


def test_a_definition_carries_its_declarations(tmp_path):
    definition = capable_definition("analyzer", ("analyze_resistance", "summarize"))
    assert definition.provides_capabilities == (
        CapabilityRef("analyze_resistance"),
        CapabilityRef("summarize"),
    )


def test_declarations_persist_and_are_queryable_from_both_ends(tmp_path):
    """Spec §9."""
    db_path = tmp_path / "p.db"

    runtime = Runtime(db_path)
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    runtime.close()

    runtime2 = Runtime(db_path)
    assert [c.name for c in runtime2.get_process_capabilities("analyzer", "1")] == [
        "analyze_resistance"
    ]
    assert runtime2.capabilities.get_processes_providing("analyze_resistance") == [
        ("analyzer", "1")
    ]
    runtime2.close()


def test_two_definitions_can_provide_the_same_capability(tmp_path):
    runtime = Runtime(tmp_path / "p.db")
    register_capable(runtime, "fast", ("analyze_resistance",))
    register_capable(runtime, "thorough", ("analyze_resistance",))

    providers = runtime.capabilities.get_processes_providing("analyze_resistance")
    assert sorted(providers) == [("fast", "1"), ("thorough", "1")]
    runtime.close()


def test_one_definition_can_provide_several_capabilities(tmp_path):
    runtime = Runtime(tmp_path / "p.db")
    register_capable(runtime, "swiss_army", ("a", "b", "c"))

    assert sorted(
        c.name for c in runtime.get_process_capabilities("swiss_army", "1")
    ) == ["a", "b", "c"]
    runtime.close()


def test_definition_versions_declare_independently(tmp_path):
    from nexus_seed.core.process import ProcessDefinition

    runtime = Runtime(tmp_path / "p.db")
    runtime.register_process(
        ProcessDefinition(
            name="analyzer",
            version="1",
            handler="a1",
            provides_capabilities=(CapabilityRef("analyze_resistance"),),
        ),
        worker,
    )
    runtime.register_process(
        ProcessDefinition(
            name="analyzer",
            version="2",
            handler="a2",
            provides_capabilities=(CapabilityRef("analyze_resistance"), CapabilityRef("extra")),
        ),
        worker,
    )

    assert len(runtime.get_process_capabilities("analyzer", "1")) == 1
    assert len(runtime.get_process_capabilities("analyzer", "2")) == 2
    runtime.close()


# --- the shipped processes -------------------------------------------------


def test_resistance_check_declares_what_it_can_do(tmp_path):
    """Spec §54: the migration of the existing work process."""
    assert RESISTANCE_CHECK.provides_capabilities == (CapabilityRef("analyze_resistance"),)


def test_write_analysis_result_declares_what_it_can_do():
    assert WRITE_ANALYSIS_RESULT.provides_capabilities == (
        CapabilityRef("generate_analysis_report"),
    )


def test_bootstrapping_registers_the_shipped_capabilities(tmp_path):
    from nexus_seed.processes.actions import bootstrap_actions
    from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence

    runtime = Runtime(tmp_path / "p.db")
    bootstrap_work_intelligence(runtime)
    bootstrap_actions(runtime)

    names = {c.name for c in runtime.list_capabilities()}
    assert names == {"analyze_resistance", "generate_analysis_report"}
    assert runtime.capabilities.get_processes_providing("analyze_resistance") == [
        ("resistance_check", "1")
    ]
    assert runtime.capabilities.get_processes_providing("generate_analysis_report") == [
        ("write_analysis_result", "1")
    ]
    runtime.close()


def test_a_definition_providing_nothing_asked_for_is_not_a_candidate(tmp_path):
    """It was never in the running, so it is not recorded as rejected."""
    runtime = Runtime(tmp_path / "p.db")
    runtime.register_process(capable_definition("plain"), worker)

    result = runtime.match_capabilities(["analyze_resistance"])

    assert not result.eligible
    assert result.candidates == []
    assert result.missing_capabilities == ["analyze_resistance"]
    runtime.close()


def test_a_partial_provider_is_a_candidate_that_fell_short(tmp_path):
    """Covering *some* of the ask is what makes a near-miss worth recording."""
    runtime = Runtime(tmp_path / "p.db")
    register_capable(runtime, "partial", ("a",))

    result = runtime.match_capabilities(["a", "b"])

    assert [c.definition_name for c in result.candidates] == ["partial"]
    assert result.candidates[0].covered_capabilities == ["a"]
    assert result.candidates[0].missing_capabilities == ["b"]
    runtime.close()
