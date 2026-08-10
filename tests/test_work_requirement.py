"""WorkRequirement model + store: persistence, work_key uniqueness, status."""

from __future__ import annotations

import uuid

from nexus_seed.storage.database import Database
from nexus_seed.storage.work_requirement_store import WorkRequirementStore
from nexus_seed.work.work_requirement import WorkRequirement, WorkStatus


def _store(tmp_path) -> WorkRequirementStore:
    return WorkRequirementStore(Database(tmp_path / "work.db"))


def test_save_and_get(tmp_path):
    store = _store(tmp_path)
    req = WorkRequirement(
        work_type="resistance_check",
        work_key="resistance_check:D1_CD:v1",
        related_entities=["D1_CD"],
        reason="D1_CD.target changed",
        source_state_delta_id=uuid.uuid4(),
        priority=80,
    )
    assert store.save(req) is True

    fetched = store.get(req.id)
    assert fetched is not None
    assert fetched.work_type == "resistance_check"
    assert fetched.related_entities == ["D1_CD"]
    assert fetched.status is WorkStatus.EXPECTED
    assert fetched.priority == 80


def test_work_key_is_unique(tmp_path):
    store = _store(tmp_path)
    key = "resistance_check:D1_CD:v1"
    first = WorkRequirement(work_type="resistance_check", work_key=key)
    second = WorkRequirement(work_type="resistance_check", work_key=key)

    assert store.save(first) is True
    assert store.save(second) is False  # ignored: same work_key
    assert store.get_by_work_key(key).id == first.id
    assert len(store.all()) == 1


def test_status_transitions(tmp_path):
    store = _store(tmp_path)
    req = WorkRequirement(work_type="resistance_check", work_key="rc:D1_CD:v1")
    store.save(req)

    store.update_status(req.id, WorkStatus.SPAWNED)
    assert store.get(req.id).status is WorkStatus.SPAWNED
    store.update_status(req.id, WorkStatus.SATISFIED)
    assert store.get(req.id).status is WorkStatus.SATISFIED
    assert [r.id for r in store.by_status(WorkStatus.SATISFIED)] == [req.id]
