"""Phase 6 asks the Runtime what is being pursued, not the Control Plane.

The point of the seam is that "what NEXUS SEED is currently pursuing" is a
question the Runtime relays and a *domain* answers.  Today the Control Plane
answers "ACTIVE Goals"; when the Orchestrator takes over it will answer "live
Projects", and nothing in Phase 6 should notice the difference.  These tests
pin that independence rather than the current answer.
"""

from __future__ import annotations

import uuid

import pytest

from nexus_seed.control.models import Goal
from nexus_seed.core.event import Event
from nexus_seed.presence import project_self
from nexus_seed.processes.control import bootstrap_control
from nexus_seed.processes.persistent_being import bootstrap_persistent_being
from nexus_seed.runtime.runtime import Runtime


pytestmark = pytest.mark.asyncio


def active_goal(title: str = "Ship the thing") -> Goal:
    return Goal(title=title, objective=title.lower(), owner_identity_id="master-1")


async def test_runtime_pursues_nothing_until_a_domain_says_otherwise(tmp_path):
    runtime = Runtime(tmp_path / "bare.db")
    try:
        assert runtime.active_pursuits() == []
        assert project_self(runtime).active_goal_ids == ()
    finally:
        runtime.close()


async def test_control_plane_registers_active_goals_as_the_pursuit_source(tmp_path):
    runtime = Runtime(tmp_path / "control.db")
    try:
        bootstrap_control(runtime)
        goal = active_goal()
        runtime.control_store.save_goal(goal)
        assert runtime.active_pursuits() == [goal.id]
        assert project_self(runtime).active_goal_ids == (goal.id,)
    finally:
        runtime.close()


async def test_completed_goals_stop_being_pursued(tmp_path):
    runtime = Runtime(tmp_path / "completed.db")
    try:
        bootstrap_control(runtime)
        goal = active_goal()
        runtime.control_store.save_goal(goal)
        runtime.control_store.update_goal_status(goal.id, "COMPLETED")
        assert runtime.active_pursuits() == []
    finally:
        runtime.close()


async def test_phase6_reads_a_source_that_has_nothing_to_do_with_goals(tmp_path):
    """The Orchestrator switch is a registration change, not a Phase 6 change."""

    runtime = Runtime(tmp_path / "projects.db")
    try:
        project_ids = [uuid.uuid4(), uuid.uuid4()]
        runtime.register_pursuit_source("orchestrator.projects", lambda: list(project_ids))
        assert runtime.active_pursuits() == project_ids
        assert project_self(runtime).active_goal_ids == tuple(project_ids)
        # No Goal was ever created, and no Control Plane bootstrapped.
        assert runtime.control_store.goals() == []
    finally:
        runtime.close()


async def test_registering_the_same_name_twice_replaces_the_source(tmp_path):
    runtime = Runtime(tmp_path / "replace.db")
    try:
        runtime.register_pursuit_source("domain", lambda: ["first"])
        runtime.register_pursuit_source("domain", lambda: ["second"])
        assert runtime.active_pursuits() == ["second"]
        bootstrap_control(runtime)
        bootstrap_control(runtime)
        goal = active_goal()
        runtime.control_store.save_goal(goal)
        assert runtime.active_pursuits() == ["second", goal.id]
    finally:
        runtime.close()


async def test_one_broken_source_does_not_blind_the_others(tmp_path):
    runtime = Runtime(tmp_path / "broken.db")
    try:
        def explode() -> list:
            raise RuntimeError("source is down")

        runtime.register_pursuit_source("broken", explode)
        runtime.register_pursuit_source("working", lambda: ["still-here"])
        assert runtime.active_pursuits() == ["still-here"]
    finally:
        runtime.close()


async def test_startup_wake_counts_pursuits_from_the_seam(tmp_path):
    runtime = Runtime(tmp_path / "wake.db")
    try:
        runtime.register_pursuit_source("orchestrator.projects", lambda: ["project-1"])
        assert bootstrap_persistent_being(runtime, enabled=True, wake_on_start=True) is True
        wakeups = [event for event in runtime.event_store.all() if event.type == "existence_wakeup"]
        assert len(wakeups) == 1
        assert wakeups[0].payload["active_goal_count"] == 1
    finally:
        runtime.close()


async def test_startup_stays_quiet_when_nothing_is_pursued(tmp_path):
    runtime = Runtime(tmp_path / "quiet.db")
    try:
        assert bootstrap_persistent_being(runtime, enabled=True, wake_on_start=True) is True
        assert [
            event for event in runtime.event_store.all() if event.type == "existence_wakeup"
        ] == []
    finally:
        runtime.close()


async def test_phase6_modules_do_not_read_the_control_store(tmp_path):
    from pathlib import Path

    for module in ("nexus_seed/presence/projections.py", "nexus_seed/processes/persistent_being.py"):
        assert "control_store" not in Path(module).read_text(), module
