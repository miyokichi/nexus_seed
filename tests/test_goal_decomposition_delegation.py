"""Goal decomposition through the ordinary Work path and a real A2A agent.

The seam under test is that decomposition stopped being a special case.  A Goal
with no success criteria states a need — ``work_generation`` — and the existing
matcher, spawner, ProviderSelector and ProviderRegistry decide where that need
is met.  The LLM backend keeps the job only when nothing else can do it.
"""

from __future__ import annotations

import json

from nexus_seed.backends.llm import FakeLLMBackend, proposal_response
from nexus_seed.control.models import Goal
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.federation_config import FederationSettings, configure_external_agents
from nexus_seed.processes.control import (
    GOAL_DECOMPOSITION_FAILED,
    GOAL_DECOMPOSITION_PROPOSED,
    GOAL_DECOMPOSITION_REQUEST_SOURCE,
    WORK_GENERATION_CAPABILITY,
    bootstrap_control,
)
from nexus_seed.processes.persistent_being import bootstrap_persistent_being
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.providers import ProviderInvocationStatus
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus
from tests.a2a_helpers import FakeA2AServer, data_artifact, rpc_error, task
from tests.capability_helpers import work_pipeline

#: What the planner agent answers with, in the Skill's own vocabulary.
WORK_CANDIDATES = {
    "work_candidates": [
        {
            "work_type": "data_ingestion",
            "reason": "load the sales export before anything can be measured",
            "required_capabilities": ["load_csv_file"],
        },
        {
            "work_type": "statistical_analysis",
            "reason": "quantify the month over month change",
            "required_capabilities": ["calculate_percentage_change"],
        },
    ]
}

#: A decomposition the LLM backend would produce, in the proposal vocabulary.
LLM_DECOMPOSITION = {
    "work": [{
        "semantic_key": "llm_written_work",
        "objective": "do the thing the LLM decided on",
        "work_type": "llm_written_work",
        "required_capabilities": [{"name": "some_llm_named_capability"}],
    }]
}


def skill_root(tmp_path):
    """Write the work-generation Skill into a project-local skills root."""
    package = tmp_path / "skills" / "work_generation"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "# Work Generation\n\nPropose the Work a Goal still needs.", encoding="utf-8"
    )
    (package / "skill.json").write_text(
        json.dumps({
            "name": "work_generation",
            "version": "1",
            "description": "Propose the Work a Goal still needs",
            "capabilities": [WORK_GENERATION_CAPABILITY],
            "input_ports": ["goal", "world_state", "work_inventory"],
            "output_ports": ["work_candidate"],
            "output_schema": {"type": "object"},
        }),
        encoding="utf-8",
    )
    return tmp_path / "skills"


def federation(root, url):
    """Bind the work-generation Skill to one A2A planner agent."""
    return FederationSettings.from_dict({
        "providers": {
            "planner_agent": {"type": "a2a", "url": url, "poll_interval_seconds": 0.01}
        },
        "bindings": {WORK_GENERATION_CAPABILITY: {"provider": "planner_agent"}},
        "skills": {"roots": [str(root)]},
    })


def goal_runtime(tmp_path, name, *, backend=None):
    """A runtime that can evaluate a Goal and run the Work that comes of it."""
    runtime = work_pipeline(Runtime(tmp_path / name))
    bootstrap_semantic(runtime)
    bootstrap_control(runtime)
    if backend is not None:
        runtime.register_backend("llm", backend)
    bootstrap_persistent_being(runtime, enabled=True, wake_on_start=False)
    return runtime


def open_goal(runtime, objective="explain the sales drop"):
    """A durable Goal with no success criteria, so it needs decomposing."""
    goal = Goal(title="Sales drop", objective=objective, owner_identity_id="master-1")
    runtime.control_store.save_goal(goal)
    return goal


def decomposition_request(runtime, goal):
    """The WorkRequirement that asked for this Goal's decomposition, if any."""
    return [
        work
        for work in runtime.work_requirement_store.for_goal(goal.id)
        if work.metadata.get("source") == GOAL_DECOMPOSITION_REQUEST_SOURCE
    ]


def generated_work(runtime, goal):
    """The WorkRequirements the decomposition produced."""
    return [
        work
        for work in runtime.work_requirement_store.for_goal(goal.id)
        if work.metadata.get("source") == "goal_decomposition"
    ]


def completed_agent(artifacts):
    """A planner agent that answers one message/send with ``artifacts``."""
    return FakeA2AServer({"message/send": [task("completed", artifacts=artifacts)]})


# --- A: nothing configured -------------------------------------------------


async def test_without_work_generation_the_llm_still_decomposes(tmp_path):
    backend = FakeLLMBackend(default=proposal_response(LLM_DECOMPOSITION))
    runtime = goal_runtime(tmp_path, "no-skill.db", backend=backend)
    goal = open_goal(runtime)
    try:
        await runtime.submit_event(Event("goal_created", "test", {"goal_id": str(goal.id)}))

        assert backend.calls, "the LLM backend is still the fallback decomposer"
        assert decomposition_request(runtime, goal) == []
        works = generated_work(runtime, goal)
        assert [work.work_type for work in works] == ["llm_written_work"]
        assert runtime.provider_store.invocations() == []
    finally:
        runtime.close()


# --- B: the capability exists and is delegated -----------------------------


async def test_a_configured_provider_decomposes_instead_of_the_llm(tmp_path):
    root = skill_root(tmp_path)
    backend = FakeLLMBackend(default=proposal_response(LLM_DECOMPOSITION))
    with completed_agent([data_artifact(WORK_CANDIDATES)]) as server:
        runtime = goal_runtime(tmp_path, "delegated.db", backend=backend)
        configure_external_agents(runtime, settings=federation(root, server.url))
        goal = open_goal(runtime)
        try:
            await runtime.submit_event(
                Event("goal_created", "test", {"goal_id": str(goal.id)})
            )

            assert backend.calls == [], "decomposition must not call the LLM directly"
            requests = decomposition_request(runtime, goal)
            assert len(requests) == 1
            assert [item.name for item in requests[0].required_capabilities] == [
                WORK_GENERATION_CAPABILITY
            ]
            assert requests[0].selected_definition_name == "work_generation"

            instance = next(
                item
                for item in runtime.process_store.all_instances()
                if item.definition_name == "work_generation"
            )
            assert instance.status is ProcessStatus.COMPLETED

            invocation = runtime.provider_store.invocations()[0]
            assert invocation.status is ProviderInvocationStatus.COMPLETED
            selections = [
                item
                for item in runtime.provider_store.all_selections()
                if item.process_definition_name == "work_generation"
            ]
            assert selections and selections[0].provider_id is not None

            # The need, not the Skill's generic name, is what the agent was told.
            sent = server.calls("message/send")[0]["params"]
            assert goal.objective in sent["message"]["parts"][0]["data"]["instruction"]
            assert sent["metadata"]["nexus_seed/goal_id"] == str(goal.id)
        finally:
            runtime.close()


# --- C: the structured result becomes ordinary Work ------------------------


async def test_work_candidates_become_ordinary_work_requirements(tmp_path):
    root = skill_root(tmp_path)
    with completed_agent([data_artifact(WORK_CANDIDATES)]) as server:
        runtime = goal_runtime(tmp_path, "candidates.db")
        configure_external_agents(runtime, settings=federation(root, server.url))
        goal = open_goal(runtime)
        try:
            await runtime.submit_event(
                Event("goal_created", "test", {"goal_id": str(goal.id)})
            )

            proposed = runtime.event_store.by_type(GOAL_DECOMPOSITION_PROPOSED)
            assert len(proposed) == 1
            assert proposed[0].payload["source"] == "provider"

            works = sorted(generated_work(runtime, goal), key=lambda w: w.work_type)
            assert [work.work_type for work in works] == [
                "data_ingestion",
                "statistical_analysis",
            ]
            assert [item.name for item in works[0].required_capabilities] == [
                "load_csv_file"
            ]
            # ``reason`` is the Skill's word for the objective a need records.
            assert works[0].objective == (
                "load the sales export before anything can be measured"
            )
            assert works[0].goal_id == goal.id
            assert runtime.work_requirement_store.get(
                decomposition_request(runtime, goal)[0].id
            ).status is WorkStatus.SATISFIED
        finally:
            runtime.close()


async def test_two_candidates_of_one_work_type_stay_distinct(tmp_path):
    root = skill_root(tmp_path)
    twice = {
        "work_candidates": [
            {"work_type": "data_ingestion", "reason": "load sales",
             "required_capabilities": ["load_csv_file"]},
            {"work_type": "data_ingestion", "reason": "load returns",
             "required_capabilities": ["load_csv_file"]},
        ]
    }
    with completed_agent([data_artifact(twice)]) as server:
        runtime = goal_runtime(tmp_path, "duplicate-type.db")
        configure_external_agents(runtime, settings=federation(root, server.url))
        goal = open_goal(runtime)
        try:
            await runtime.submit_event(
                Event("goal_created", "test", {"goal_id": str(goal.id)})
            )

            works = generated_work(runtime, goal)
            assert len(works) == 2
            assert len({work.work_key for work in works}) == 2
            assert {work.metadata["semantic_key"] for work in works} == {
                "data_ingestion",
                "data_ingestion_2",
            }
        finally:
            runtime.close()


async def test_an_unusable_provider_answer_fails_the_decomposition(tmp_path):
    root = skill_root(tmp_path)
    backend = FakeLLMBackend(default=proposal_response(LLM_DECOMPOSITION))
    nonsense = {"work_candidates": [{"reason": "no work_type at all"}]}
    with completed_agent([data_artifact(nonsense)]) as server:
        runtime = goal_runtime(tmp_path, "bad-answer.db", backend=backend)
        configure_external_agents(runtime, settings=federation(root, server.url))
        goal = open_goal(runtime)
        try:
            await runtime.submit_event(
                Event("goal_created", "test", {"goal_id": str(goal.id)})
            )

            assert runtime.event_store.by_type(GOAL_DECOMPOSITION_FAILED)
            assert generated_work(runtime, goal) == []
            assert backend.calls == [], "a bad answer is not a reason to ask an LLM"
        finally:
            runtime.close()


# --- D: idempotency --------------------------------------------------------


async def test_re_evaluating_a_goal_does_not_request_decomposition_twice(tmp_path):
    root = skill_root(tmp_path)
    with completed_agent([data_artifact(WORK_CANDIDATES)]) as server:
        runtime = goal_runtime(tmp_path, "idempotent.db")
        configure_external_agents(runtime, settings=federation(root, server.url))
        goal = open_goal(runtime)
        try:
            for _ in range(3):
                await runtime.submit_event(
                    Event("goal_evaluation_requested", "test", {
                        "goal_id": str(goal.id),
                        "intention_id": "forced",
                    })
                )

            assert len(decomposition_request(runtime, goal)) == 1
            assert len(generated_work(runtime, goal)) == 2
            assert len(server.calls("message/send")) == 1
            assert len(runtime.provider_store.invocations()) == 1
        finally:
            runtime.close()


# --- E: provider failure after selection -----------------------------------


async def test_a_failed_delegation_does_not_silently_fall_back_to_the_llm(tmp_path):
    root = skill_root(tmp_path)
    backend = FakeLLMBackend(default=proposal_response(LLM_DECOMPOSITION))
    with FakeA2AServer({"message/send": [rpc_error("planner exploded")]}) as server:
        runtime = goal_runtime(tmp_path, "provider-failure.db", backend=backend)
        configure_external_agents(runtime, settings=federation(root, server.url))
        goal = open_goal(runtime)
        try:
            await runtime.submit_event(
                Event("goal_created", "test", {"goal_id": str(goal.id)})
            )
            # Anything that would re-evaluate the Goal must not re-decide it.
            await runtime.submit_event(
                Event("goal_evaluation_requested", "test", {
                    "goal_id": str(goal.id), "intention_id": "forced",
                })
            )

            assert backend.calls == [], "a provider failure is not an LLM fallback"
            assert len(decomposition_request(runtime, goal)) == 1
            assert generated_work(runtime, goal) == []
            invocation = runtime.provider_store.invocations()[0]
            assert invocation.status is ProviderInvocationStatus.FAILED
            assert runtime.event_store.by_type(GOAL_DECOMPOSITION_PROPOSED) == []
        finally:
            runtime.close()


# --- the trigger stays narrow ----------------------------------------------


async def test_an_unrelated_provider_completion_is_not_a_decomposition(tmp_path):
    """Another Skill finishing must not be read as a Goal decomposition."""
    package = tmp_path / "skills" / "world_event_interpretation"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("# Interpret\n", encoding="utf-8")
    (package / "skill.json").write_text(
        json.dumps({
            "name": "world_event_interpretation",
            "version": "1",
            "description": "Interpret an Observation",
            "capabilities": ["world_event_interpretation"],
            "input_ports": ["observation"],
            "output_ports": ["state_delta_candidate"],
        }),
        encoding="utf-8",
    )
    candidate = {"type": "state_delta_candidate", "value": {"entity": "D1_CD"}}
    with completed_agent([data_artifact(candidate)]) as server:
        runtime = goal_runtime(tmp_path, "unrelated.db")
        configure_external_agents(runtime, settings=FederationSettings.from_dict({
            "providers": {
                "observer_agent": {
                    "type": "a2a", "url": server.url, "poll_interval_seconds": 0.01
                }
            },
            "bindings": {"world_event_interpretation": {"provider": "observer_agent"}},
            "skills": {"roots": [str(tmp_path / "skills")]},
        }))
        from tests.capability_helpers import make_work, offer_work

        work = make_work(
            runtime,
            work_key="interpret:obs-1",
            required=("world_event_interpretation",),
        )
        try:
            await offer_work(runtime, work)

            assert runtime.provider_store.invocations()[0].status is (
                ProviderInvocationStatus.COMPLETED
            )
            assert runtime.event_store.by_type(GOAL_DECOMPOSITION_PROPOSED) == []
            assert runtime.event_store.by_type(GOAL_DECOMPOSITION_FAILED) == []
        finally:
            runtime.close()
