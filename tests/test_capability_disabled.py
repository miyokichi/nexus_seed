"""AT9 + AT10 (spec §65, §66, §40–§43): withdrawing a competence.

Disabling is how a capability is taken out of service without losing what it
did — a delete would break the audit trail that Phase 4A exists to create.  It
affects *future* matching only: work already running is left alone (spec §43).
"""

from __future__ import annotations

from capability_helpers import (
    capability_runtime,
    instances_named,
    make_work,
    offer_work,
    register_capable,
    status_of,
)

from nexus_seed.capabilities.models import CapabilityMatchStatus
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


def test_a_disabled_capability_is_not_a_candidate(tmp_path):
    """AT9."""
    runtime = Runtime(tmp_path / "d.db")
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    runtime.set_capability_enabled("analyze_resistance", "1", False)

    result = runtime.match_capabilities(["analyze_resistance"])

    assert result.status is CapabilityMatchStatus.MISSING_CAPABILITY
    assert result.missing_capabilities == ["analyze_resistance"]
    assert not result.eligible
    runtime.close()


def test_a_disabled_process_is_not_a_candidate(tmp_path):
    """AT10: the capability is fine; the process offering it is not available."""
    runtime = Runtime(tmp_path / "d.db")
    register_capable(runtime, "analyzer", ("analyze_resistance",), enabled=False)

    result = runtime.match_capabilities(["analyze_resistance"])

    assert not result.eligible
    assert result.candidates == []
    # Nothing considered could cover it, so it reads as missing, not composable.
    assert result.status is CapabilityMatchStatus.MISSING_CAPABILITY
    runtime.close()


def test_another_provider_still_wins_when_one_is_disabled(tmp_path):
    runtime = Runtime(tmp_path / "d.db")
    register_capable(runtime, "retired", ("analyze_resistance",), enabled=False, priority=99)
    register_capable(runtime, "current", ("analyze_resistance",))

    result = runtime.match_capabilities(["analyze_resistance"])

    assert result.selected.definition_name == "current"
    runtime.close()


async def test_disable_stops_new_matching_only(tmp_path):
    """Spec §43: withdrawing a competence does not touch work already done."""
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "analyzer", ("analyze_resistance",))

    running = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, running)
    assert status_of(runtime, running) is WorkStatus.SATISFIED
    completed_instances = len(instances_named(runtime, "analyzer"))

    runtime.set_capability_enabled("analyze_resistance", "1", False)

    later = make_work(runtime, work_key="w2", required=("analyze_resistance",))
    await offer_work(runtime, later)

    assert status_of(runtime, later) is WorkStatus.BLOCKED_CAPABILITY
    # The earlier work is untouched.
    assert status_of(runtime, running) is WorkStatus.SATISFIED
    assert len(instances_named(runtime, "analyzer")) == completed_instances
    runtime.close()


async def test_a_suspended_process_is_not_disturbed_by_disabling(tmp_path):
    from nexus_seed.capabilities.models import CapabilityRef
    from nexus_seed.context.requirements import ContextRequirements, ContinuationReq
    from nexus_seed.core.process import ProcessDefinition

    async def waits(ctx):
        if ctx.resume_point == "resumed":
            ctx.satisfy_work()
            return ctx.complete(output={"done": True})
        return ctx.suspend(
            resume_point="resumed",
            waiting_for={"event_type": "wake_up"},
            saved_process_state={},
        )

    runtime = capability_runtime(tmp_path)
    runtime.register_process(
        ProcessDefinition(
            name="waiter",
            version="1",
            handler="waiter",
            provides_capabilities=(CapabilityRef("analyze_resistance"),),
            context_requirements=ContextRequirements(
                include_trigger_event=True, continuation=ContinuationReq(include=True)
            ),
        ),
        waits,
    )

    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, work)
    instance = instances_named(runtime, "waiter")[0]
    assert instance.status is ProcessStatus.SUSPENDED

    runtime.set_capability_enabled("analyze_resistance", "1", False)

    from nexus_seed.core.event import Event

    await runtime.submit_event(Event("wake_up", "test", {}))

    assert runtime.process_store.get_instance(instance.id).status is ProcessStatus.COMPLETED
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


def test_disabling_preserves_the_record(tmp_path):
    """Spec §42: a flag, not a delete."""
    runtime = Runtime(tmp_path / "d.db")
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    runtime.set_capability_enabled("analyze_resistance", "1", False)

    assert runtime.get_capability("analyze_resistance", "1").enabled is False
    assert len(runtime.list_capabilities()) == 1
    assert runtime.list_capabilities(enabled_only=True) == []
    # The declaration relation is still on record.
    assert runtime.get_process_capabilities("analyzer", "1") != []
    runtime.close()
