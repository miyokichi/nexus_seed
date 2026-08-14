"""Capability models (spec §5, §6, §14, §92).

The distinction the whole phase rests on: a capability is what a Process can
*accomplish*, not what a tool can mechanically *do*.  Those are two registries
on purpose (spec §4).
"""

from __future__ import annotations

import uuid

from nexus_seed.capabilities.models import (
    CandidateMatch,
    Capability,
    CapabilityMatchStatus,
    CapabilityRef,
    CapabilityRequirement,
    CapabilityWorkMatch,
    MatchResult,
)


def test_a_capability_is_identified_by_name_and_version():
    capability = Capability(name="analyze_resistance", version="2")
    assert capability.key == ("analyze_resistance", "2")
    assert capability.ref == CapabilityRef("analyze_resistance", "2")
    assert capability.enabled is True


def test_versions_of_one_capability_are_distinct_things():
    v1 = Capability(name="analyze_resistance", version="1")
    v2 = Capability(name="analyze_resistance", version="2")
    assert v1.key != v2.key
    assert v1.id != v2.id


def test_a_requirement_with_no_constraint_accepts_any_version():
    requirement = CapabilityRequirement(name="analyze_resistance")
    assert requirement.satisfied_by(Capability(name="analyze_resistance", version="1"))
    assert requirement.satisfied_by(Capability(name="analyze_resistance", version="7"))


def test_a_version_constraint_is_exact():
    requirement = CapabilityRequirement(name="analyze_resistance", version_constraint="2")
    assert requirement.satisfied_by(Capability(name="analyze_resistance", version="2"))
    assert not requirement.satisfied_by(Capability(name="analyze_resistance", version="1"))


def test_a_disabled_capability_satisfies_nothing():
    """Disabling is how a competence is withdrawn without losing its history."""
    requirement = CapabilityRequirement(name="analyze_resistance")
    disabled = Capability(name="analyze_resistance", enabled=False)
    assert not requirement.satisfied_by(disabled)


def test_a_different_name_never_satisfies():
    requirement = CapabilityRequirement(name="analyze_resistance")
    assert not requirement.satisfied_by(Capability(name="summarize_report"))


def test_requirements_round_trip_and_coerce():
    original = CapabilityRequirement(
        name="analyze_resistance",
        version_constraint="2",
        required=False,
        metadata={"why": "nice to have"},
    )
    restored = CapabilityRequirement.from_dict(original.to_dict())
    assert restored == original

    assert CapabilityRequirement.coerce("analyze_resistance").name == "analyze_resistance"
    assert CapabilityRequirement.coerce(original) is original
    assert CapabilityRequirement.coerce({"name": "x"}).name == "x"


def test_only_matched_single_process_is_eligible():
    """The three outcomes call for different responses (spec §92)."""
    assert CapabilityMatchStatus.MATCHED_SINGLE_PROCESS.eligible
    assert not CapabilityMatchStatus.MISSING_CAPABILITY.eligible
    assert not CapabilityMatchStatus.COMPOSITION_REQUIRED.eligible


def test_a_candidate_records_what_it_can_and_cannot_do():
    candidate = CandidateMatch(
        definition_name="analyzer",
        definition_version="1",
        covered_capabilities=["a"],
        missing_capabilities=["b"],
    )
    assert candidate.key == ("analyzer", "1")
    assert not candidate.eligible
    assert candidate.to_dict()["missing"] == ["b"]


def test_a_match_result_is_only_eligible_with_a_selection():
    assert not MatchResult(status=CapabilityMatchStatus.MISSING_CAPABILITY).eligible
    selected = CandidateMatch("analyzer", "1", eligible=True)
    assert MatchResult(
        status=CapabilityMatchStatus.MATCHED_SINGLE_PROCESS, selected=selected
    ).eligible


def test_an_audit_record_captures_the_whole_attempt():
    requirement_id = uuid.uuid4()
    reqs = [CapabilityRequirement(name="a"), CapabilityRequirement(name="b")]
    selected = CandidateMatch("analyzer", "2", covered_capabilities=["a", "b"], eligible=True)
    result = MatchResult(
        status=CapabilityMatchStatus.MATCHED_SINGLE_PROCESS,
        selected=selected,
        candidates=[selected],
        reasons=["chose analyzer"],
    )

    record = CapabilityWorkMatch.from_result(requirement_id, reqs, result)

    assert record.work_requirement_id == requirement_id
    assert record.selected_definition_name == "analyzer"
    assert record.selected_definition_version == "2"
    assert [r["name"] for r in record.required_capabilities] == ["a", "b"]
    assert record.reasons == ["chose analyzer"]


def test_a_capability_ref_reads_well():
    assert str(CapabilityRef("analyze_resistance", "2")) == "analyze_resistance:v2"
    assert CapabilityRef("analyze_resistance").version == "1"


def test_capabilities_are_not_backend_capabilities():
    """Two different registries, deliberately (spec §4)."""
    from nexus_seed.backends.action import BackendCapabilities

    assert Capability(name="write_file") != BackendCapabilities(backend="local_file")
    assert not hasattr(Capability(name="x"), "actions")
    assert not hasattr(BackendCapabilities(backend="b"), "enabled")
