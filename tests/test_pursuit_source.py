"""Phase 6 asks the Runtime what is being pursued, and a domain answers.

The point of the seam is that "what NEXUS SEED is currently pursuing" is a
question the Runtime relays and a *domain* answers.  The Project Orchestrator
answers it today (see ``test_project_pursuits``); these tests pin the seam
itself, with sources that belong to no domain at all.
"""

from __future__ import annotations

import pytest

from nexus_seed.presence import project_self
from nexus_seed.processes.persistent_being import bootstrap_persistent_being
from nexus_seed.pursuit import Pursuit, PursuitSource
from nexus_seed.runtime.runtime import Runtime


pytestmark = pytest.mark.asyncio


class ListSource(PursuitSource):
    """A source backed by a fixed list, standing in for a real domain."""

    def __init__(self, pursuits):
        self.pursuits = list(pursuits)

    def live(self):
        return [item for item in self.pursuits if item.active]

    def get(self, pursuit_id):
        return next((item for item in self.pursuits if item.id == str(pursuit_id)), None)


async def test_runtime_pursues_nothing_until_a_domain_says_otherwise(tmp_path):
    runtime = Runtime(tmp_path / "bare.db")
    try:
        assert runtime.active_pursuits() == []
        assert runtime.get_pursuit("anything") is None
        assert project_self(runtime).active_pursuit_ids == ()
    finally:
        runtime.close()


async def test_a_registered_source_is_what_the_runtime_reports(tmp_path):
    runtime = Runtime(tmp_path / "registered.db")
    try:
        pursuits = [
            Pursuit(id="p-1", objective="調べる"),
            Pursuit(id="p-2", objective="直す"),
        ]
        runtime.register_pursuit_source("domain", ListSource(pursuits))

        assert runtime.active_pursuits() == pursuits
        assert project_self(runtime).active_pursuit_ids == ("p-1", "p-2")
        assert runtime.get_pursuit("p-2").objective == "直す"
    finally:
        runtime.close()


async def test_a_finished_pursuit_is_still_resolvable(tmp_path):
    """An Intention outlives the thing it is about, so ``get`` must not filter."""

    runtime = Runtime(tmp_path / "finished.db")
    try:
        done = Pursuit(id="p-1", objective="調べる", active=False)
        runtime.register_pursuit_source("domain", ListSource([done]))

        assert runtime.active_pursuits() == []
        assert runtime.get_pursuit("p-1") == done
    finally:
        runtime.close()


async def test_registering_the_same_name_twice_replaces_the_source(tmp_path):
    runtime = Runtime(tmp_path / "replace.db")
    try:
        first = Pursuit(id="first", objective="a")
        second = Pursuit(id="second", objective="b")
        runtime.register_pursuit_source("domain", ListSource([first]))
        runtime.register_pursuit_source("domain", ListSource([second]))

        assert runtime.active_pursuits() == [second]
        assert runtime.get_pursuit("first") is None
    finally:
        runtime.close()


async def test_sources_registered_under_different_names_all_answer(tmp_path):
    runtime = Runtime(tmp_path / "two.db")
    try:
        runtime.register_pursuit_source("a", ListSource([Pursuit("a-1", "x")]))
        runtime.register_pursuit_source("b", ListSource([Pursuit("b-1", "y")]))

        assert [item.id for item in runtime.active_pursuits()] == ["a-1", "b-1"]
        assert runtime.get_pursuit("b-1").objective == "y"
    finally:
        runtime.close()


async def test_one_broken_source_does_not_blind_the_others(tmp_path):
    runtime = Runtime(tmp_path / "broken.db")
    try:
        class Broken(PursuitSource):
            def live(self):
                raise RuntimeError("source is down")

            def get(self, pursuit_id):
                raise RuntimeError("source is down")

        runtime.register_pursuit_source("broken", Broken())
        runtime.register_pursuit_source("working", ListSource([Pursuit("ok", "b")]))

        assert [item.id for item in runtime.active_pursuits()] == ["ok"]
        assert runtime.get_pursuit("ok").objective == "b"
    finally:
        runtime.close()


async def test_startup_wake_counts_pursuits_from_the_seam(tmp_path):
    runtime = Runtime(tmp_path / "wake.db")
    try:
        runtime.register_pursuit_source("domain", ListSource([Pursuit("p-1", "調べる")]))
        assert bootstrap_persistent_being(runtime, enabled=True, wake_on_start=True) is True

        wakeups = [
            event for event in runtime.event_store.all() if event.type == "existence_wakeup"
        ]
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


async def test_phase6_modules_never_name_a_domain_store(tmp_path):
    from pathlib import Path

    for module in (
        "nexus_seed/presence/projections.py",
        "nexus_seed/processes/persistent_being.py",
    ):
        source = Path(module).read_text()
        assert "control_store" not in source, module
        assert "get_goal" not in source, module
