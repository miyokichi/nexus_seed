"""Phase C: the Cockpit shows the Project Orchestrator's own projects.

Read-only, and deliberately apart from the Goal-derived projection: two
different things called "project" in one list would leave a person unable to
tell which one is authoritative.
"""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.cockpit import CockpitService
from nexus_seed.orchestrator import (
    A2AMessageType,
    InProcessAgentRuntime,
    ProjectOrchestrator,
    escalation,
    status_message,
)
from nexus_seed.processes.project_orchestration import bootstrap_project_orchestration
from nexus_seed.runtime.runtime import Runtime

REQUEST = "SAPの前年同期と比較して"


def cockpit(runtime) -> CockpitService:
    return CockpitService(runtime, phase6_enabled=True, master_id="local-operator")


def orchestrator(runtime, behaviour=None):
    return ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(behaviour=behaviour),
        backend=FakeLLMBackend(
            default=proposal_response(
                {
                    "action": "CREATE_PROJECT",
                    "proposed_goal": REQUEST,
                    "reason": "new",
                    "confidence": 0.9,
                }
            )
        ),
    )


def blocked(config, envelope):
    return [
        status_message(config.project_id, "6月と7月を比較した"),
        escalation(
            config.project_id,
            A2AMessageType.NEED_CAPABILITY,
            required_capability="sap_sales_data_access",
            reason="SAP accessがない",
        ),
    ]


async def blocked_runtime(tmp_path):
    runtime = Runtime(tmp_path / "app.db")
    orch = orchestrator(runtime, blocked)
    bootstrap_project_orchestration(runtime, orch, enabled=True)
    await orch.handle_request(REQUEST)
    return runtime, orch, orch.projects.all()[0]


async def test_cockpit_lists_orchestrator_projects(tmp_path):
    runtime, _orch, project = await blocked_runtime(tmp_path)

    section = cockpit(runtime).snapshot()["orchestrator"]

    assert section["enabled"] is True
    assert section["counts"] == {"BLOCKED": 1}
    [row] = section["projects"]
    assert row["id"] == project.id
    assert row["goal"] == REQUEST
    assert row["status"] == "BLOCKED"
    assert row["summary"] == "6月と7月を比較した"
    assert row["assigned_agent_id"] == project.assigned_agent_id
    assert row["parent_project_id"] is None
    assert row["created_at"] and row["updated_at"]
    runtime.close()


async def test_cockpit_project_detail_shows_agent_and_blocker(tmp_path):
    runtime, _orch, project = await blocked_runtime(tmp_path)

    detail = cockpit(runtime).orchestrator_project(project.id)

    assert detail["goal"] == REQUEST
    assert detail["status"] == "BLOCKED"
    [blocker] = detail["blockers"]
    assert blocker["kind"] == "NEED_CAPABILITY"
    assert blocker["detail"]["required_capability"] == "sap_sales_data_access"
    assert detail["agent"]["agent_id"] == project.assigned_agent_id
    assert detail["agent"]["runtime"] == "in_process"
    assert detail["agent"]["assignment"]["kind"] == "ASSIGN_GOAL"
    runtime.close()


async def test_cockpit_shows_recent_a2a_audit(tmp_path):
    runtime, _orch, project = await blocked_runtime(tmp_path)

    detail = cockpit(runtime).orchestrator_project(project.id)

    assert [m["type"] for m in detail["messages"]] == [
        "PROJECT_STATUS",
        "NEED_CAPABILITY",
    ]
    assert {m["direction"] for m in detail["messages"]} == {"inbound"}
    runtime.close()


async def test_an_unknown_project_is_not_invented(tmp_path):
    runtime, _orch, _project = await blocked_runtime(tmp_path)

    assert cockpit(runtime).orchestrator_project("project-nope") is None
    runtime.close()


async def test_legacy_and_orchestrator_projects_are_not_ambiguously_mixed(tmp_path):
    runtime, _orch, project = await blocked_runtime(tmp_path)

    snapshot = cockpit(runtime).snapshot()

    # Two separate sections, and the orchestrator's project is only in its own.
    orchestrator_ids = {row["id"] for row in snapshot["orchestrator"]["projects"]}
    legacy_ids = {row.get("project_id") for row in snapshot["projects"]}
    assert project.id in orchestrator_ids
    assert orchestrator_ids & legacy_ids == set()
    runtime.close()


async def test_the_section_says_when_the_orchestrator_is_off(tmp_path):
    runtime = Runtime(tmp_path / "app.db")
    orch = orchestrator(runtime)
    bootstrap_project_orchestration(runtime, orch, enabled=False)

    section = cockpit(runtime).snapshot()["orchestrator"]

    # Off is not the same as "nothing is happening", and says so.
    assert section["enabled"] is False
    assert section["projects"] == []
    runtime.close()
