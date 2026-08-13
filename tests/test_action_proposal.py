"""ActionProposal / ActionExecution model + persistence (spec §5-§9, §26-§27)."""

from __future__ import annotations

import uuid

from nexus_seed.actions.models import (
    ActionExecution,
    ActionExecutionStatus,
    ActionProposal,
    ActionProposalStatus,
    RiskLevel,
)
from nexus_seed.storage.action_execution_store import ActionExecutionStore
from nexus_seed.storage.action_proposal_store import ActionProposalStore
from nexus_seed.storage.database import Database


def test_new_proposal_is_pending_and_self_rooted():
    proposal = ActionProposal(backend="python", action_type="write_file")
    assert proposal.status is ActionProposalStatus.PENDING
    assert proposal.root_proposal_id == proposal.id
    assert proposal.replaces_proposal_id is None
    # An idempotency key is derived, never left empty (spec §33).
    assert str(proposal.id) in proposal.idempotency_key


def test_idempotency_key_is_proposal_scoped():
    """Two proposals for the same effect are distinct intentions."""
    a = ActionProposal(backend="python", action_type="write_file", target="/tmp/x")
    b = ActionProposal(backend="python", action_type="write_file", target="/tmp/x")
    assert a.idempotency_key != b.idempotency_key


def test_explicit_idempotency_key_is_kept():
    proposal = ActionProposal(
        backend="python", action_type="write_file", idempotency_key="report:2026-08"
    )
    assert proposal.idempotency_key == "report:2026-08"


def test_from_dict_treats_unknown_risk_as_critical():
    """An unparseable risk level must never be read as 'safe'."""
    proposal = ActionProposal.from_dict(
        {"backend": "python", "action_type": "write_file", "risk_level": "probably fine"}
    )
    assert proposal.risk_level is RiskLevel.CRITICAL


def test_from_dict_rejects_non_dict_input():
    assert ActionProposal.from_dict("rm -rf /") is None


def test_proposal_round_trips_through_sqlite(tmp_path):
    db = Database(tmp_path / "p.db")
    store = ActionProposalStore(db)
    process_id = uuid.uuid4()
    proposal = ActionProposal(
        backend="python",
        action_type="write_file",
        target="/tmp/result.txt",
        parameters={"content": "analysis completed"},
        required_permissions=["filesystem.write"],
        declared_side_effects=["filesystem_write"],
        risk_level=RiskLevel.MEDIUM,
        rationale="because",
        created_by_process_id=process_id,
        context_snapshot_id=uuid.uuid4(),
    )
    store.save(proposal)

    loaded = store.get(proposal.id)
    assert loaded.backend == "python"
    assert loaded.parameters == {"content": "analysis completed"}
    assert loaded.required_permissions == ["filesystem.write"]
    assert loaded.declared_side_effects == ["filesystem_write"]
    assert loaded.risk_level is RiskLevel.MEDIUM
    assert loaded.created_by_process_id == process_id
    assert loaded.context_snapshot_id == proposal.context_snapshot_id
    assert loaded.idempotency_key == proposal.idempotency_key
    assert loaded.root_proposal_id == proposal.id
    db.close()


def test_proposal_survives_status_changes_and_is_never_deleted(tmp_path):
    """A proposal outlives its execution — the record of intent is permanent."""
    db = Database(tmp_path / "p.db")
    store = ActionProposalStore(db)
    proposal = ActionProposal(backend="python", action_type="write_file")
    store.save(proposal)

    for status in (
        ActionProposalStatus.APPROVED,
        ActionProposalStatus.EXECUTING,
        ActionProposalStatus.SUCCEEDED,
    ):
        store.update_status(proposal.id, status)

    assert store.get(proposal.id).status is ActionProposalStatus.SUCCEEDED
    assert len(store.all()) == 1
    assert store.by_status(ActionProposalStatus.SUCCEEDED) != []
    db.close()


def test_execution_round_trips_and_finds_succeeded_by_key(tmp_path):
    db = Database(tmp_path / "e.db")
    store = ActionExecutionStore(db)
    proposal_id, instance_id = uuid.uuid4(), uuid.uuid4()

    failed = ActionExecution(
        action_proposal_id=proposal_id,
        process_instance_id=instance_id,
        backend="fake_action",
        action_type="write_file",
        status=ActionExecutionStatus.FAILED,
        attempt=1,
        idempotency_key="k1",
        error="timeout",
    )
    succeeded = ActionExecution(
        action_proposal_id=proposal_id,
        process_instance_id=instance_id,
        backend="fake_action",
        action_type="write_file",
        status=ActionExecutionStatus.SUCCEEDED,
        attempt=2,
        idempotency_key="k1",
        result={"bytes_written": 3},
    )
    store.save(failed)
    store.save(succeeded)

    history = store.for_proposal(proposal_id)
    assert [e.attempt for e in history] == [1, 2]
    assert history[0].error == "timeout"
    assert history[1].result == {"bytes_written": 3}

    assert store.succeeded_for_key("k1").id == succeeded.id
    assert store.succeeded_for_key("other") is None
    assert store.succeeded_for_key(None) is None
    db.close()
