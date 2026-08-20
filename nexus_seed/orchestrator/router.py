"""ProjectRouter — does this belong to a running Project, or is it a new one?

This is the one genuinely semantic decision NEXUS SEED makes for itself, so it
is made by a reasoning backend over the compiled :class:`RoutingContext` — not
by string matching against goals.  The backend only *proposes*: the decision is
schema-checked and checked against the real project list before it is acted on,
the same boundary the LLM interpretation path uses.

Without a backend (or when one fails or answers unusably) the router falls back
to a deterministic, conservative decision: treat the request as its own Goal.
Creating a separate project is recoverable; silently burying a request inside an
unrelated one is not.
"""

from __future__ import annotations

import logging
from typing import Any

from ..backends.base import BackendRequest, ExecutionBackend
from .models import RoutingAction, RoutingContext, RoutingDecision

logger = logging.getLogger("nexus_seed.orchestrator.router")

INSTRUCTION = (
    "You route an incoming request for a project orchestrator.\n"
    "Decide whether it belongs to one of the projects already under way or is "
    "an independent goal of its own.\n"
    "- ADD_TASK_TO_PROJECT: it is more work for an existing project's goal. "
    "Set target_project_id and proposed_task.\n"
    "- CREATE_PROJECT: it is an independent goal. Set proposed_goal.\n"
    "- UPDATE_PROJECT: it only changes an existing project's framing or "
    "priority. Set target_project_id.\n"
    "- IGNORE: it needs no project work at all.  A question about how a "
    "project is going, why it stopped, or what is left is one of these: "
    "answering it changes nothing, so it is not work.\n"
    "A project listed as BLOCKED or WAITING_HUMAN is waiting for a person. If "
    "the request answers what one of them is waiting on — supplying what was "
    "missing, dropping the requirement, or telling it how to proceed without "
    "it — that is ADD_TASK_TO_PROJECT on that project, not a new goal. Read "
    "each project's blockers to judge this.\n"
    "Judge by meaning, not by shared words. Answer with JSON only."
)

DECISION_SCHEMA = {
    "type": "object",
    "required": ["action", "reason", "confidence"],
    "properties": {
        "action": {
            "type": "string",
            "enum": [action.value for action in RoutingAction],
        },
        "target_project_id": {"type": ["string", "null"]},
        "proposed_goal": {"type": ["string", "null"]},
        "proposed_task": {"type": ["string", "null"]},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
}


class ProjectRouter:
    """Turns an incoming request into a :class:`RoutingDecision`."""

    def __init__(self, backend: ExecutionBackend | None = None) -> None:
        self.backend = backend

    async def route(self, context: RoutingContext) -> RoutingDecision:
        """Decide what to do with ``context.request``."""
        if self.backend is None:
            return self._fallback(context, "no reasoning backend configured")

        request = BackendRequest(
            instruction=INSTRUCTION,
            context=context.to_dict(),
            output_schema=DECISION_SCHEMA,
            metadata={"source": context.source, "origin_project_id": context.origin_project_id},
        )
        try:
            result = await self.backend.execute(request)
        except Exception as exc:  # noqa: BLE001 - routing must still answer
            logger.exception("routing backend raised")
            return self._fallback(context, f"routing backend raised: {exc}")

        if not result.success:
            return self._fallback(context, f"routing backend failed: {result.error}")

        decision = self._parse(result.parsed_output)
        if decision is None:
            # Say what came back.  This is the one decision NEXUS SEED makes
            # for itself, and "unusable output" with nothing else to go on
            # leaves no way to tell a bad prompt from a bad model.
            logger.warning(
                "routing backend returned unusable output: %r",
                result.parsed_output if result.parsed_output is not None else result.raw_output,
            )
            return self._fallback(context, "routing backend returned unusable output")
        return self._validate(decision, context)

    # --- parsing and validation -------------------------------------------

    @staticmethod
    def _parse(data: Any) -> RoutingDecision | None:
        """Build a decision from backend output, or ``None`` if unusable."""
        if not isinstance(data, dict):
            return None
        raw_action = data.get("action")
        try:
            action = RoutingAction(raw_action)
        except ValueError:
            return None
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return RoutingDecision(
            action=action,
            target_project_id=_clean(data.get("target_project_id")),
            proposed_goal=_clean(data.get("proposed_goal")),
            proposed_task=_clean(data.get("proposed_task")),
            reason=str(data.get("reason") or ""),
            confidence=confidence,
        )

    def _validate(self, decision: RoutingDecision, context: RoutingContext) -> RoutingDecision:
        """Check a proposed decision against the projects that really exist."""
        known = {project["id"] for project in context.active_projects}

        if decision.action in (RoutingAction.ADD_TASK_TO_PROJECT, RoutingAction.UPDATE_PROJECT):
            if decision.target_project_id not in known:
                logger.warning(
                    "routing backend named project %r; known: %s",
                    decision.target_project_id,
                    sorted(known),
                )
                return self._fallback(
                    context,
                    f"{decision.action.value} named unknown project "
                    f"{decision.target_project_id!r}",
                )
            if decision.action is RoutingAction.ADD_TASK_TO_PROJECT and not decision.proposed_task:
                decision.proposed_task = context.request

        if decision.action is RoutingAction.CREATE_PROJECT and not decision.proposed_goal:
            decision.proposed_goal = context.request

        return decision

    @staticmethod
    def _fallback(context: RoutingContext, reason: str) -> RoutingDecision:
        """The conservative decision when meaning cannot be judged.

        A request that came from inside a project belongs to that project: the
        person was looking at it when they wrote this.  Making a second project
        instead would split one goal in two and hand it to a second Agent, so
        an unroutable in-project request becomes a Task on its own project.
        Only a request with no origin is its own Goal.
        """
        logger.info("routing fallback: %s", reason)
        if context.origin_project_id:
            return RoutingDecision(
                action=RoutingAction.ADD_TASK_TO_PROJECT,
                target_project_id=context.origin_project_id,
                proposed_task=context.request,
                reason=f"fallback ({reason})",
                confidence=0.0,
            )
        return RoutingDecision(
            action=RoutingAction.CREATE_PROJECT,
            proposed_goal=context.request,
            reason=f"fallback ({reason})",
            confidence=0.0,
        )


def _clean(value: Any) -> str | None:
    """Return a non-empty stripped string, or ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = ["DECISION_SCHEMA", "INSTRUCTION", "ProjectRouter"]
