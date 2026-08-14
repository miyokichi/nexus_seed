"""AT16 (spec §72, §46–§48): why was *that* process chosen, and when?

A name lookup needs no justification — there was only ever one answer.  Once
selection becomes a decision, it has to be defensible: which candidates were
weighed, what each was missing, and why the winner won.
"""

from __future__ import annotations

from capability_helpers import (
    capability_runtime,
    make_work,
    offer_work,
    register_capable,
    status_of,
)

from nexus_seed.capabilities.models import CapabilityMatchStatus
from nexus_seed.work.work_requirement import WorkStatus


async def test_a_successful_match_records_the_whole_deliberation(tmp_path):
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "plain", ("analyze_resistance",))
    register_capable(runtime, "preferred", ("analyze_resistance",), priority=10)
    register_capable(runtime, "irrelevant", ("something_else",))
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))

    await offer_work(runtime, work)

    attempt = runtime.get_capability_matches(work.id)[0]
    assert attempt.status is CapabilityMatchStatus.MATCHED_SINGLE_PROCESS
    assert attempt.selected_definition_name == "preferred"
    assert [r["name"] for r in attempt.required_capabilities] == ["analyze_resistance"]

    # Only the two that could plausibly have done it; ``irrelevant`` provides
    # nothing that was asked for and was never in the running.
    considered = {c["definition_name"] for c in attempt.candidates}
    assert considered == {"plain", "preferred"}

    by_name = {c["definition_name"]: c for c in attempt.candidates}
    assert by_name["preferred"]["eligible"] is True
    assert by_name["preferred"]["score"] > by_name["plain"]["score"]
    assert any("selected" in r for r in by_name["preferred"]["reasons"])
    runtime.close()


async def test_a_blocked_match_records_what_was_missing(tmp_path):
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "partial", ("a",))
    work = make_work(runtime, work_key="w1", required=("a", "z"))

    await offer_work(runtime, work)

    attempt = runtime.get_capability_matches(work.id)[0]
    assert attempt.status is CapabilityMatchStatus.MISSING_CAPABILITY
    assert attempt.missing_capabilities == ["z"]
    assert attempt.selected_definition_name is None
    assert "no process provides ['z']" in attempt.reasons[0]
    # The near-miss is on record too.
    assert attempt.candidates[0]["covered"] == ["a"]
    assert attempt.candidates[0]["missing"] == ["z"]
    runtime.close()


async def test_the_composition_boundary_is_recorded_explicitly(tmp_path):
    """The input Phase 4B will start from."""
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "p1", ("a",))
    register_capable(runtime, "p2", ("b",))
    work = make_work(runtime, work_key="w1", required=("a", "b"))

    await offer_work(runtime, work)

    attempt = runtime.get_capability_matches(work.id)[0]
    assert attempt.status is CapabilityMatchStatus.COMPOSITION_REQUIRED
    assert attempt.missing_capabilities == []
    assert "no single process covers them all" in attempt.reasons[0]
    # Both partial providers are named, ready to be composed later.
    partials = {c["definition_name"]: c["covered"] for c in attempt.candidates}
    assert partials == {"p1": ["a"], "p2": ["b"]}
    runtime.close()


async def test_the_selection_is_kept_on_the_requirement_too(tmp_path):
    """So spawning does not have to re-decide (spec §46)."""
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))

    await offer_work(runtime, work)

    stored = runtime.work_requirement_store.get(work.id)
    assert stored.selected_definition_name == "analyzer"
    assert stored.selected_definition_version == "1"
    assert stored.missing_capabilities == []
    runtime.close()


async def test_attempts_accumulate_across_a_blocked_then_matched_life(tmp_path):
    runtime = capability_runtime(tmp_path)
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, work)

    register_capable(runtime, "analyzer", ("analyze_resistance",))
    await runtime.run_pending()

    attempts = runtime.get_capability_matches(work.id)
    assert len(attempts) == 2
    assert attempts[0].created_at <= attempts[1].created_at
    # The first attempt is unchanged by the second.
    assert attempts[0].missing_capabilities == ["analyze_resistance"]
    assert attempts[0].selected_definition_name is None
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


async def test_legacy_work_records_no_capability_attempt(tmp_path):
    """Work that declares nothing takes the old path, and says so by silence."""
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    work = make_work(runtime, work_key="w1", work_type="resistance_check", required=())

    await offer_work(runtime, work)

    assert runtime.get_capability_matches(work.id) == []
    assert runtime.work_requirement_store.get(work.id).selected_definition_name is None
    runtime.close()
