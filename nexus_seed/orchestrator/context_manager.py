"""ContextManager — compile what the ProjectRouter is allowed to reason over.

The router must decide "does this belong to something already running?", so it
needs the live projects and their state — but not the whole database.  This
builds one compact, regenerable :class:`RoutingContext` per decision, in a fixed
order so the same facts always read the same way.

Live means every project that still needs an Agent, so a BLOCKED or
WAITING_HUMAN one is offered too: a person answering what a project is stuck on
must reach *that* project rather than start a new one.

What the user asked for before is already here, in compressed form — a project's
goal is a request, and its open tasks are the follow-ups it has been given — so
no separate history of requests is kept or replayed at the router.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .models import Project, RoutingContext
from .project_manager import ProjectManager

logger = logging.getLogger("nexus_seed.orchestrator.context_manager")

#: How many finished projects are summarised for the router.
RECENT_SUMMARY_LIMIT = 5


class ContextManager:
    """Assembles routing context from projects, world state and user context."""

    def __init__(
        self,
        projects: ProjectManager,
        *,
        world_state_provider: Callable[[], dict[str, Any]] | None = None,
        user_context_provider: Callable[[], dict[str, Any]] | None = None,
        recent_summary_limit: int = RECENT_SUMMARY_LIMIT,
    ) -> None:
        self.projects = projects
        self.world_state_provider = world_state_provider
        self.user_context_provider = user_context_provider
        self.recent_summary_limit = recent_summary_limit

    def build(
        self,
        request: str,
        *,
        source: str = "user",
        user_context: dict[str, Any] | None = None,
        origin_project_id: str | None = None,
    ) -> RoutingContext:
        """Compile the context for one routing decision."""
        # Ordered by priority, so the most urgent projects are read first.
        live = self.projects.live()
        active_projects = [project.to_routing_dict() for project in live]

        finished = [p for p in self.projects.all() if not p.is_live]
        finished.sort(key=lambda p: p.updated_at, reverse=True)
        recent = [
            {"id": p.id, "goal": p.goal, "status": p.status.value, "summary": p.summary}
            for p in finished[: self.recent_summary_limit]
        ]

        context = RoutingContext(
            request=request,
            source=source,
            world_state=self._world_state(),
            active_projects=active_projects,
            recent_project_summaries=recent,
            user_context=dict(user_context or self._user_context()),
            origin_project_id=origin_project_id,
        )
        logger.debug(
            "routing context: %d active, %d recent", len(active_projects), len(recent)
        )
        return context

    def _world_state(self) -> dict[str, Any]:
        if self.world_state_provider is None:
            return {}
        try:
            return dict(self.world_state_provider() or {})
        except Exception:  # noqa: BLE001 - context must not break routing
            logger.exception("world state provider failed; routing without it")
            return {}

    def _user_context(self) -> dict[str, Any]:
        if self.user_context_provider is None:
            return {}
        try:
            return dict(self.user_context_provider() or {})
        except Exception:  # noqa: BLE001
            logger.exception("user context provider failed; routing without it")
            return {}


__all__ = ["ContextManager", "RECENT_SUMMARY_LIMIT"]
