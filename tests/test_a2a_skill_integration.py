"""Skill -> Capability -> Provider -> external A2A agent, over real HTTP.

The point of this test is the seam: NEXUS SEED decides *what* must be done and
keeps the durable record, an external agent process does the thinking, and
neither imports the other.
"""

from __future__ import annotations

import json
import uuid

from nexus_seed.core.process import ProcessStatus
from nexus_seed.federation_config import (
    FederationSettings,
    configure_external_agents,
)
from nexus_seed.providers import ProviderInvocation, ProviderInvocationStatus
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus
from tests.a2a_helpers import FakeA2AServer, data_artifact, rpc_error, task
from tests.capability_helpers import make_work, offer_work, work_pipeline

CANDIDATE = {
    "type": "state_delta_candidate",
    "value": {
        "entity": "D1_CD",
        "attribute": "status",
        "new_value": "open",
        "confidence": 0.8,
    },
}


def skill_root(tmp_path):
    """Write the interpretation Skill into a project-local skills root."""
    package = tmp_path / "skills" / "world_event_interpretation"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "# World Event Interpretation\n\nCompare the Observation with World State.",
        encoding="utf-8",
    )
    (package / "skill.json").write_text(
        json.dumps(
            {
                "name": "world_event_interpretation",
                "version": "1",
                "description": "Interpret an Observation",
                "capabilities": ["world_event_interpretation"],
                "input_ports": ["observation"],
                "output_ports": ["state_delta_candidate"],
                "output_schema": {"type": "object"},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path / "skills"


def settings(root, url, **overrides):
    raw = {
        "providers": {"observer_agent": {"type": "a2a", "url": url, "poll_interval_seconds": 0.01}},
        "bindings": {"world_event_interpretation": {"provider": "observer_agent"}},
        "skills": {"roots": [str(root)]},
    }
    raw.update(overrides)
    return FederationSettings.from_dict(raw)


async def test_work_reaches_an_external_agent_and_comes_back_typed(tmp_path):
    root = skill_root(tmp_path)
    with FakeA2AServer(
        {
            "message/send": [task("submitted")],
            "tasks/get": [
                task("working"),
                task("completed", artifacts=[data_artifact(CANDIDATE)]),
            ],
        }
    ) as server:
        runtime = work_pipeline(Runtime(tmp_path / "a2a.db"))
        report = configure_external_agents(
            runtime, settings=settings(root, server.url)
        )
        work = make_work(
            runtime,
            work_key="interpret:obs-42",
            required=("world_event_interpretation",),
        )

        await offer_work(runtime, work)

        assert report.unroutable == []
        instance = next(
            i
            for i in runtime.process_store.all_instances()
            if i.definition_name == "world_event_interpretation"
        )
        assert instance.status is ProcessStatus.COMPLETED
        assert runtime.work_requirement_store.get(work.id).status is WorkStatus.SATISFIED

        invocation = runtime.provider_store.invocations()[0]
        assert invocation.status is ProviderInvocationStatus.COMPLETED
        assert invocation.result_snapshot["typed_outputs"] == [CANDIDATE]
        assert invocation.external_run_id == "task-1"

        sent = server.calls("message/send")[0]["params"]
        data = sent["message"]["parts"][0]["data"]
        assert data["instruction"].startswith("# World Event Interpretation")
        assert data["output_schema"] == {"type": "object"}
        assert sent["metadata"]["nexus_seed/work_id"] == str(work.id)
        runtime.close()


async def test_a_dead_agent_blocks_the_work_instead_of_losing_it(tmp_path):
    root = skill_root(tmp_path)
    with FakeA2AServer({"message/send": [task("completed")]}) as server:
        url = server.url
    # The agent is gone before any work is offered.
    runtime = work_pipeline(Runtime(tmp_path / "a2a.db"))
    configure_external_agents(runtime, settings=settings(root, url))
    work = make_work(
        runtime, work_key="interpret:obs-43", required=("world_event_interpretation",)
    )

    await offer_work(runtime, work)

    reloaded = runtime.work_requirement_store.get(work.id)
    assert reloaded.status is not WorkStatus.SATISFIED
    provider = runtime.provider_store.get_provider(
        runtime.provider_store.get_provider_by_name("observer_agent", "1").id
    )
    assert provider.health.value in {"UNAVAILABLE", "DEGRADED"}
    runtime.close()


async def test_swapping_the_remote_agent_is_a_configuration_change(tmp_path):
    root = skill_root(tmp_path)
    with FakeA2AServer(
        {"message/send": [task("completed", artifacts=[data_artifact(CANDIDATE)])]}
    ) as first, FakeA2AServer(
        {"message/send": [task("completed", artifacts=[data_artifact(CANDIDATE)])]}
    ) as second:
        for index, server in enumerate((first, second)):
            runtime = work_pipeline(Runtime(tmp_path / f"swap-{index}.db"))
            configure_external_agents(runtime, settings=settings(root, server.url))
            work = make_work(
                runtime,
                work_key=f"interpret:obs-{index}",
                required=("world_event_interpretation",),
            )

            await offer_work(runtime, work)

            assert (
                runtime.work_requirement_store.get(work.id).status
                is WorkStatus.SATISFIED
            )
            assert server.calls("message/send")
            runtime.close()


async def test_a_skill_with_no_configured_provider_is_reported_not_dropped(tmp_path):
    root = skill_root(tmp_path)
    runtime = Runtime(tmp_path / "a2a.db")

    report = configure_external_agents(
        runtime,
        settings=FederationSettings.from_dict({"skills": {"roots": [str(root)]}}),
    )

    assert report.unroutable == ["world_event_interpretation"]
    assert runtime.get_definition("world_event_interpretation", "1") is None
    runtime.close()


def waiting_invocation(runtime, provider, *, external_run_id="task-1"):
    """A delegation that is in flight at the remote agent."""
    return runtime.provider_store.save_invocation(
        ProviderInvocation(
            provider_id=provider.id,
            provider_binding_id=uuid.uuid4(),
            process_instance_id=uuid.uuid4(),
            request_snapshot={},
            idempotency_key=f"activation:{external_run_id}",
            status=ProviderInvocationStatus.WAITING_EXTERNAL,
            external_run_id=external_run_id,
        )
    )


async def test_cancelling_an_invocation_tells_the_remote_agent(tmp_path):
    root = skill_root(tmp_path)
    with FakeA2AServer(
        {"message/send": [task("submitted")], "tasks/cancel": [task("canceled")]}
    ) as server:
        runtime = Runtime(tmp_path / "a2a.db")
        report = configure_external_agents(runtime, settings=settings(root, server.url))
        invocation = waiting_invocation(runtime, report.providers["observer_agent"])

        assert await runtime.cancel_provider_invocation(invocation.id) is True

        assert server.calls("tasks/cancel")[0]["params"] == {"id": "task-1"}
        stored = runtime.provider_store.get_invocation(invocation.id)
        assert stored.status is ProviderInvocationStatus.CANCELLED
        runtime.close()


async def test_a_refused_remote_cancel_still_cancels_our_own_record(tmp_path):
    root = skill_root(tmp_path)
    with FakeA2AServer(
        {"message/send": [task("submitted")], "tasks/cancel": [rpc_error("unknown task")]}
    ) as server:
        runtime = Runtime(tmp_path / "a2a.db")
        report = configure_external_agents(runtime, settings=settings(root, server.url))
        invocation = waiting_invocation(runtime, report.providers["observer_agent"])

        assert await runtime.cancel_provider_invocation(invocation.id) is True

        stored = runtime.provider_store.get_invocation(invocation.id)
        assert stored.status is ProviderInvocationStatus.CANCELLED
        runtime.close()


async def test_disabled_configuration_changes_nothing(tmp_path):
    runtime = Runtime(tmp_path / "a2a.db")

    assert configure_external_agents(runtime, settings=FederationSettings()) is None
    assert [p.name for p in runtime.get_execution_providers()] == ["local_runtime"]
    runtime.close()
