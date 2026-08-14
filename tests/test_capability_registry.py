"""AT1/AT2 through the Runtime (spec §10–§12, §57, §58, §83).

The registry is the system's self-model: what it can do, as opposed to what it
knows about the world.  It survives a restart, and re-registering a definition
announces nothing new.
"""

from __future__ import annotations

from capability_helpers import capable_definition, register_capable, worker

from nexus_seed.capabilities.models import Capability, CapabilityRef, CapabilityRequirement
from nexus_seed.capabilities.registry import as_refs, version_sort_key
from nexus_seed.runtime.runtime import Runtime


def test_registering_a_process_declares_its_capabilities(tmp_path):
    """AT2."""
    runtime = Runtime(tmp_path / "c.db")
    register_capable(runtime, "resistance_analyzer", ("analyze_resistance",))

    provided = runtime.get_process_capabilities("resistance_analyzer", "1")
    assert [c.name for c in provided] == ["analyze_resistance"]
    assert runtime.capabilities.get_processes_providing("analyze_resistance") == [
        ("resistance_analyzer", "1")
    ]
    runtime.close()


def test_a_declared_capability_is_created_on_demand(tmp_path):
    """A process introduces a competence simply by claiming it."""
    runtime = Runtime(tmp_path / "c.db")
    assert runtime.get_capability("analyze_resistance") is None

    register_capable(runtime, "resistance_analyzer", ("analyze_resistance",))

    capability = runtime.get_capability("analyze_resistance")
    assert capability is not None and capability.enabled
    runtime.close()


def test_the_registry_survives_a_restart(tmp_path):
    """AT1: matching must answer the same way after a rebuild (spec §9)."""
    db_path = tmp_path / "c.db"

    runtime = Runtime(db_path)
    register_capable(runtime, "resistance_analyzer", ("analyze_resistance",))
    runtime.close()

    runtime2 = Runtime(db_path)
    # Nothing re-registered yet — the registry is read from SQLite.
    assert [c.name for c in runtime2.list_capabilities()] == ["analyze_resistance"]
    assert runtime2.get_process_capabilities("resistance_analyzer", "1") != []
    runtime2.close()


def test_re_registering_announces_nothing_new(tmp_path):
    """Spec §83: availability fires on change, not on every startup."""
    db_path = tmp_path / "c.db"

    runtime = Runtime(db_path)
    register_capable(runtime, "resistance_analyzer", ("analyze_resistance",))
    first = len(runtime.event_store.by_type("capability_available"))
    assert first == 1
    runtime.close()

    runtime2 = Runtime(db_path)
    register_capable(runtime2, "resistance_analyzer", ("analyze_resistance",))
    assert len(runtime2.event_store.by_type("capability_available")) == first
    runtime2.close()


def test_a_second_capability_on_an_existing_process_is_new(tmp_path):
    runtime = Runtime(tmp_path / "c.db")
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    register_capable(runtime, "analyzer", ("analyze_resistance", "summarize_report"))

    announced = [
        e.payload["capability_name"]
        for e in runtime.event_store.by_type("capability_available")
    ]
    assert announced == ["analyze_resistance", "summarize_report"]
    assert len(runtime.get_process_capabilities("analyzer", "1")) == 2
    runtime.close()


def test_re_declaring_replaces_the_previous_declaration(tmp_path):
    """A definition that stops claiming something stops providing it."""
    runtime = Runtime(tmp_path / "c.db")
    register_capable(runtime, "analyzer", ("analyze_resistance", "summarize_report"))
    register_capable(runtime, "analyzer", ("analyze_resistance",))

    provided = [c.name for c in runtime.get_process_capabilities("analyzer", "1")]
    assert provided == ["analyze_resistance"]
    # The capability itself still exists; nobody provides it now.
    assert runtime.get_capability("summarize_report") is not None
    assert runtime.capabilities.get_processes_providing("summarize_report") == []
    runtime.close()


def test_a_capability_can_be_registered_without_a_process(tmp_path):
    runtime = Runtime(tmp_path / "c.db")
    runtime.register_capability(
        Capability(name="summarize_report", description="condense a document")
    )

    capability = runtime.get_capability("summarize_report")
    assert capability.description == "condense a document"
    assert runtime.capabilities.get_processes_providing("summarize_report") == []
    # It exists but nothing provides it: that is a gap, not a match.
    assert not runtime.capabilities.is_provided(
        CapabilityRequirement(name="summarize_report")
    )
    runtime.close()


def test_is_provided_reflects_enablement(tmp_path):
    runtime = Runtime(tmp_path / "c.db")
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    requirement = CapabilityRequirement(name="analyze_resistance")

    assert runtime.capabilities.is_provided(requirement)
    runtime.set_capability_enabled("analyze_resistance", "1", False)
    assert not runtime.capabilities.is_provided(requirement)
    runtime.close()


def test_version_preference_is_numeric_not_lexical(tmp_path):
    """``"10"`` must beat ``"9"`` (spec §44)."""
    runtime = Runtime(tmp_path / "c.db")
    runtime.register_capability(Capability(name="c", version="9"))
    runtime.register_capability(Capability(name="c", version="10"))

    assert runtime.get_capability("c").version == "10"
    runtime.close()


def test_version_sort_key_handles_non_numeric_versions():
    assert version_sort_key("2") > version_sort_key("1")
    assert version_sort_key("1.10") > version_sort_key("1.9")
    # Non-numeric versions sort below numeric ones, but stably.
    assert version_sort_key("beta") < version_sort_key("1")
    assert version_sort_key("beta") < version_sort_key("gamma")


def test_declarations_accept_several_shapes():
    refs = as_refs(
        ["a", CapabilityRef("b", "2"), ("c", "3"), {"name": "d", "version": "4"}]
    )
    assert [(r.name, r.version) for r in refs] == [
        ("a", "1"),
        ("b", "2"),
        ("c", "3"),
        ("d", "4"),
    ]
    assert as_refs(None) == ()


def test_a_process_without_declarations_registers_normally(tmp_path):
    runtime = Runtime(tmp_path / "c.db")
    runtime.register_process(capable_definition("plain"), worker)

    assert runtime.get_process_capabilities("plain", "1") == []
    assert runtime.event_store.by_type("capability_available") == []
    runtime.close()


def test_enabling_a_disabled_capability_announces_availability(tmp_path):
    """Spec §84: regaining a competence is new availability."""
    runtime = Runtime(tmp_path / "c.db")
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    runtime.set_capability_enabled("analyze_resistance", "1", False)
    before = len(runtime.event_store.by_type("capability_available"))

    assert runtime.set_capability_enabled("analyze_resistance", "1", True) is True

    assert len(runtime.event_store.by_type("capability_available")) == before + 1
    # A no-op transition announces nothing.
    assert runtime.set_capability_enabled("analyze_resistance", "1", True) is False
    runtime.close()
