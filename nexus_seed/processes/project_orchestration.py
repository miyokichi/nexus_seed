"""Requests reach the Project Orchestrator the same way everything else does.

`nexus-seed project` is the explicit door.  This is the ordinary one: whatever
the world says — a CLI task, a webhook, a connector — arrives as a
``human_message`` Event through the existing Ingress, and one ordinary Process
hands it to the :class:`~nexus_seed.orchestrator.ProjectOrchestrator`::

    CLI / webhook / connector -> Ingress -> human_message -> route_request_to_project
                                                          -> ProjectRouter

Nothing about ingress changes.  Deduplication still belongs to the Ingress
boundary (one ``source_event_key`` is one Event is one activation), so the
router never has to wonder whether it has seen a request before, and this
Process holds no idempotency logic of its own.

This is the application's single request entry path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..core.process import ProcessContext, ProcessDefinition, ProcessResult
if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..orchestrator import ProjectOrchestrator

logger = logging.getLogger("nexus_seed.processes.project_orchestration")

ROUTE_REQUEST = ProcessDefinition(
    name="route_request_to_project",
    version="1",
    handler="route_request_to_project",
    trigger_event_types=("human_message",),
    max_retries=2,
    metadata={"role": "project_orchestrator_entry"},
)


async def route_request_to_project(ctx: ProcessContext) -> ProcessResult:
    """Give one incoming message to the Project Orchestrator to route.

    The Process does no routing itself and creates no Project: it carries the
    request across the boundary and records what the orchestrator decided.
    """
    orchestrator = ctx.project_orchestrator
    if orchestrator is None:  # pragma: no cover - invalid application wiring
        return ctx.fail("no Project Orchestrator is configured")

    payload = (ctx.event.payload if ctx.event else None) or {}
    request = str(payload.get("text") or "").strip()
    if not request:
        # Not a failure: some messages simply carry nothing to act on.
        return ctx.complete({"routed": False, "reason": "the message carried no text"})

    decision, project = await orchestrator.submit(
        request,
        source=(ctx.event.source if ctx.event else None) or "human_message",
        user_context={"event_id": str(ctx.event.id)} if ctx.event else None,
    )
    logger.info(
        "human_message -> %s (%s)",
        decision.action.value,
        project.id if project else "no project",
    )
    return ctx.complete(
        {
            "routed": True,
            "action": decision.action.value,
            "project_id": project.id if project else None,
            "agent_id": project.assigned_agent_id if project else None,
            "status": project.status.value if project else None,
        }
    )


def bootstrap_project_orchestration(
    runtime, orchestrator: "ProjectOrchestrator"
) -> None:
    """Install the application's Project request entry path."""
    runtime.set_project_orchestrator(orchestrator)
    runtime.register_process(ROUTE_REQUEST, route_request_to_project)
    logger.info("human messages are routed to the Project Orchestrator")


__all__ = [
    "ROUTE_REQUEST",
    "bootstrap_project_orchestration",
    "route_request_to_project",
]
