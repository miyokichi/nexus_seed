"""A2A execution provider: the HTTP boundary to an external Agent Runtime."""

from __future__ import annotations

import uuid

import pytest

from nexus_seed.providers import (
    A2AAgentAdapter,
    A2AEndpoint,
    A2AProtocolError,
    DelegationRequest,
    DelegationStatus,
    ProviderKind,
    ProviderUnavailableBeforeStart,
    a2a_provider_record,
)
from tests.a2a_helpers import (
    FakeA2AServer,
    data_artifact,
    free_url,
    http_status,
    rpc_error,
    task,
    text_artifact,
)

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"state_delta_candidates": {"type": "array"}},
}


def request(
    *,
    output_types=("state_delta_candidate",),
    output_schema=None,
    instructions="# Interpret\n\nCompare Observation with World State.",
    metadata=None,
):
    """Build one DelegationRequest the way ProviderRegistry does."""
    return DelegationRequest(
        invocation_id=uuid.uuid4(),
        process_instance_id=uuid.uuid4(),
        process_definition={
            "name": "world_event_interpretation",
            "version": "1",
            "description": "Interpret an Observation",
            "output_types": list(output_types),
            "output_schema": output_schema or {},
            "instructions": instructions,
        },
        objective="interpret observation 42",
        idempotency_key="activation-1:provider-1",
        required_capabilities=["world_event_interpretation"],
        typed_inputs={"observation": {"id": "obs-42"}},
        relevant_context={"world_state": {"door": "open"}},
        metadata=metadata or {},
    )


def adapter(server_url, **kwargs):
    settings = {"poll_interval_seconds": 0.01, "timeout_seconds": 5.0}
    settings.update(kwargs)
    return A2AAgentAdapter(A2AEndpoint(url=server_url, **settings))


# --- discovery -------------------------------------------------------------


async def test_agent_card_discovery_reports_the_remote_agent(tmp_path):
    with FakeA2AServer() as server:
        card = await adapter(server.url).agent_card()

    assert card.name == "fake-observer"
    assert card.transport == "JSONRPC"
    assert card.skills == ("interpret",)


async def test_missing_agent_card_is_a_provider_problem_not_a_capability_gap():
    with FakeA2AServer(card=None) as server:
        with pytest.raises(A2AProtocolError, match="agent card unavailable"):
            await adapter(server.url).agent_card()


def test_provider_record_keeps_the_endpoint_out_of_core_models():
    endpoint = A2AEndpoint(url="http://127.0.0.1:8801", token_env="LITTLE_AGENT_TOKEN")

    provider = a2a_provider_record("observer_agent", endpoint, priority=50)

    assert provider.kind is ProviderKind.EXTERNAL_AGENT
    assert provider.adapter_name == "a2a:observer_agent:1"
    assert provider.metadata["url"] == "http://127.0.0.1:8801"
    assert provider.metadata["token_env"] == "LITTLE_AGENT_TOKEN"


def test_endpoint_rejects_a_non_http_url():
    with pytest.raises(ValueError, match="http"):
        A2AEndpoint(url="ftp://127.0.0.1:8801")


# --- successful delegation -------------------------------------------------


async def test_data_part_becomes_a_typed_output():
    payload = {"type": "state_delta_candidate", "value": {"entity": "door"}}
    with FakeA2AServer(
        {"message/send": [task("completed", artifacts=[data_artifact(payload)])]}
    ) as server:
        result = await adapter(server.url).execute(request())

    assert result.status is DelegationStatus.COMPLETED
    assert result.typed_outputs == [payload]
    assert result.external_run_id == "task-1"


async def test_untyped_data_takes_the_single_declared_output_type():
    with FakeA2AServer(
        {
            "message/send": [
                task("completed", artifacts=[data_artifact({"entity": "door"})])
            ]
        }
    ) as server:
        result = await adapter(server.url).execute(request())

    assert result.typed_outputs == [
        {"type": "state_delta_candidate", "value": {"entity": "door"}}
    ]


async def test_untyped_data_is_not_guessed_when_several_types_are_declared():
    with FakeA2AServer(
        {"message/send": [task("completed", artifacts=[data_artifact({"a": 1})])]}
    ) as server:
        result = await adapter(server.url).execute(
            request(output_types=("state_delta_candidate", "summary"))
        )

    assert result.status is DelegationStatus.FAILED
    assert "ambiguous" in result.error


async def test_text_part_is_accepted_for_free_text_work():
    with FakeA2AServer(
        {"message/send": [task("completed", artifacts=[text_artifact("the door opened")])]}
    ) as server:
        result = await adapter(server.url).execute(request(output_types=("summary",)))

    assert result.status is DelegationStatus.COMPLETED
    assert result.typed_outputs == [{"type": "summary", "value": "the door opened"}]


async def test_output_schema_is_sent_when_structure_is_required():
    with FakeA2AServer(
        {
            "message/send": [
                task("completed", artifacts=[data_artifact({"type": "state_delta_candidate"})])
            ]
        }
    ) as server:
        await adapter(server.url).execute(request(output_schema=OUTPUT_SCHEMA))

    sent = server.calls("message/send")[0]["params"]
    data = sent["message"]["parts"][0]["data"]
    assert data["output_schema"] == OUTPUT_SCHEMA
    assert data["instruction"].startswith("# Interpret")
    assert data["instruction"].endswith("interpret observation 42")
    assert data["context"]["relevant_context"] == {"world_state": {"door": "open"}}


async def test_text_only_answer_fails_when_structure_was_required():
    with FakeA2AServer(
        {"message/send": [task("completed", artifacts=[text_artifact('{"almost": "json"}')])]}
    ) as server:
        result = await adapter(server.url).execute(request(output_schema=OUTPUT_SCHEMA))

    assert result.status is DelegationStatus.FAILED
    assert "structured DataPart" in result.error
    assert result.typed_outputs == []


async def test_a_message_reply_is_accepted_without_a_task():
    reply = {
        "kind": "message",
        "role": "agent",
        "parts": [{"kind": "data", "data": {"type": "state_delta_candidate", "value": 1}}],
    }
    with FakeA2AServer({"message/send": [reply]}) as server:
        result = await adapter(server.url).execute(request())

    assert result.status is DelegationStatus.COMPLETED
    assert result.typed_outputs[0]["value"] == 1


# --- task lifecycle --------------------------------------------------------


async def test_submitted_task_is_polled_until_completed():
    payload = {"type": "state_delta_candidate", "value": {"entity": "door"}}
    with FakeA2AServer(
        {
            "message/send": [task("submitted")],
            "tasks/get": [
                task("working"),
                task("working"),
                task("completed", artifacts=[data_artifact(payload)]),
            ],
        }
    ) as server:
        result = await adapter(server.url).execute(request())

    assert result.status is DelegationStatus.COMPLETED
    assert len(server.calls("tasks/get")) == 3


async def test_failed_task_carries_the_remote_reason():
    with FakeA2AServer(
        {"message/send": [task("failed", status_message="tool exploded")]}
    ) as server:
        result = await adapter(server.url).execute(request())

    assert result.status is DelegationStatus.FAILED
    assert result.error == "tool exploded"
    assert result.metadata["a2a_state"] == "failed"


async def test_canceled_task_is_a_failed_attempt_not_a_completion():
    with FakeA2AServer(
        {"message/send": [task("submitted")], "tasks/get": [task("canceled")]}
    ) as server:
        result = await adapter(server.url).execute(request())

    assert result.status is DelegationStatus.FAILED
    assert "canceled" in result.error


async def test_input_required_is_refused_and_cancelled_remotely():
    with FakeA2AServer(
        {"message/send": [task("input-required")], "tasks/cancel": [task("canceled")]}
    ) as server:
        result = await adapter(server.url).execute(request())

    assert result.status is DelegationStatus.FAILED
    assert "input-required" in result.error
    assert server.calls("tasks/cancel")


async def test_timeout_cancels_the_remote_task_and_fails():
    with FakeA2AServer(
        {
            "message/send": [task("submitted")],
            "tasks/get": [task("working")],
            "tasks/cancel": [task("canceled")],
        }
    ) as server:
        result = await adapter(
            server.url, timeout_seconds=0.15, poll_interval_seconds=0.02
        ).execute(request())

    assert result.status is DelegationStatus.FAILED
    assert "timed out" in result.error
    assert server.calls("tasks/cancel")[0]["params"] == {"id": "task-1"}


async def test_cancel_failure_never_breaks_our_own_cancel():
    with FakeA2AServer(
        {
            "message/send": [task("submitted")],
            "tasks/get": [task("working")],
            "tasks/cancel": [rpc_error("unknown task")],
        }
    ) as server:
        result = await adapter(
            server.url, timeout_seconds=0.1, poll_interval_seconds=0.02
        ).execute(request())

    assert result.status is DelegationStatus.FAILED
    assert "timed out" in result.error


# --- failure modes ---------------------------------------------------------


async def test_connection_failure_reports_unavailable_before_start():
    url = free_url()

    with pytest.raises(ProviderUnavailableBeforeStart):
        await adapter(url).execute(request())


async def test_rejected_send_reports_unavailable_before_start():
    with FakeA2AServer({"message/send": [rpc_error("no such skill")]}) as server:
        with pytest.raises(ProviderUnavailableBeforeStart, match="no such skill"):
            await adapter(server.url).execute(request())


async def test_http_failure_on_send_reports_unavailable_before_start():
    with FakeA2AServer({"message/send": [http_status(503)]}) as server:
        with pytest.raises(ProviderUnavailableBeforeStart):
            await adapter(server.url).execute(request())


async def test_polling_failure_after_start_is_a_failed_attempt():
    with FakeA2AServer(
        {"message/send": [task("submitted")], "tasks/get": [rpc_error("task lost")]}
    ) as server:
        result = await adapter(server.url).execute(request())

    assert result.status is DelegationStatus.FAILED
    assert "task lost" in result.error


async def test_malformed_artifact_is_rejected_rather_than_repaired():
    broken = {"artifactId": "a-1", "parts": [{"kind": "data", "data": "not-an-object"}]}
    with FakeA2AServer({"message/send": [task("completed", artifacts=[broken])]}) as server:
        result = await adapter(server.url).execute(request())

    assert result.status is DelegationStatus.FAILED
    assert "not an object" in result.error


async def test_task_without_an_id_cannot_be_polled():
    headless = {"kind": "task", "status": {"state": "working"}}
    with FakeA2AServer({"message/send": [headless]}) as server:
        result = await adapter(server.url).execute(request())

    assert result.status is DelegationStatus.FAILED
    assert "without an id" in result.error


# --- transport details -----------------------------------------------------


async def test_bearer_token_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("LITTLE_AGENT_TOKEN", "s3cret")
    with FakeA2AServer({"message/send": [task("completed")]}) as server:
        endpoint = A2AEndpoint(
            url=server.url, token_env="LITTLE_AGENT_TOKEN", poll_interval_seconds=0.01
        )
        await A2AAgentAdapter(endpoint).execute(request())

    assert server.calls("message/send")[0]["authorization"] == "Bearer s3cret"


async def test_no_authorization_header_without_a_configured_token():
    with FakeA2AServer({"message/send": [task("completed")]}) as server:
        await adapter(server.url).execute(request())

    assert server.calls("message/send")[0]["authorization"] is None


async def test_correlation_metadata_is_namespaced_and_optional():
    metadata = {
        "work_requirement_id": "work-1",
        "project_id": "project-1",
        "goal_id": "goal-1",
    }
    with FakeA2AServer({"message/send": [task("completed")]}) as server:
        sent = request(metadata=metadata)
        await adapter(server.url).execute(sent)

    params = server.calls("message/send")[0]["params"]
    assert params["metadata"]["nexus_seed/work_id"] == "work-1"
    assert params["metadata"]["nexus_seed/project_id"] == "project-1"
    assert params["metadata"]["nexus_seed/invocation_id"] == str(sent.invocation_id)
    assert params["metadata"]["nexus_seed/idempotency_key"] == sent.idempotency_key


async def test_the_request_carries_no_agent_product_specific_fields():
    with FakeA2AServer({"message/send": [task("completed")]}) as server:
        await adapter(server.url).execute(request())

    data = server.calls("message/send")[0]["params"]["message"]["parts"][0]["data"]
    assert sorted(data) == ["context", "instruction", "output_types"]
