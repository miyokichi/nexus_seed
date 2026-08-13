"""AT15 (spec §64): the file backend cannot be talked out of its sandbox.

Permission checks answer "may this process write files at all".  They say
nothing about *where*.  The sandbox is the second, independent boundary, and it
is enforced where the effect would happen — in the backend — so no amount of
creativity in a proposal can reach past it.
"""

from __future__ import annotations

from action_helpers import do_action, file_runtime

from nexus_seed.actions.models import ActionExecutionStatus, ActionProposalStatus
from nexus_seed.backends import ActionRequest, LocalFileActionBackend
from nexus_seed.runtime.runtime import Runtime

import pytest


def test_traversal_and_absolute_escapes_are_refused(tmp_path):
    root = tmp_path / "root"
    backend = LocalFileActionBackend(root)

    for target in (
        "../escape.txt",
        "a/../../escape.txt",
        str(tmp_path / "outside.txt"),
        "",
    ):
        with pytest.raises(ValueError):
            backend.resolve(target)


def test_the_root_itself_is_not_a_target(tmp_path):
    backend = LocalFileActionBackend(tmp_path / "root")
    with pytest.raises(ValueError):
        backend.resolve(str(tmp_path / "root"))


def test_paths_inside_the_root_resolve(tmp_path):
    root = tmp_path / "root"
    backend = LocalFileActionBackend(root)
    assert backend.resolve("a/b.txt") == (root / "a" / "b.txt").resolve()
    assert backend.resolve(str(root / "c.txt")) == (root / "c.txt").resolve()


async def test_escaping_write_creates_no_file_and_fails_permanently(tmp_path):
    root = tmp_path / "sandbox"
    outside = tmp_path / "escape.txt"

    runtime = Runtime(tmp_path / "sandbox.db")
    backend = file_runtime(runtime, root)

    await runtime.submit_event(
        do_action(
            backend="local_file",
            target="../escape.txt",
            parameters={"content": "should never be written"},
        )
    )

    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.FAILED
    assert not outside.exists()
    assert list(root.glob("*.txt")) == []

    # Refused once, permanently: a sandbox violation is not worth retrying.
    executions = runtime.get_action_executions(proposal.id)
    assert [e.attempt for e in executions] == [1]
    assert executions[0].status is ActionExecutionStatus.FAILED
    assert "escapes allowed_root" in executions[0].error
    assert len(backend.calls) == 1
    runtime.close()


async def test_absolute_path_outside_the_root_is_refused(tmp_path):
    root = tmp_path / "sandbox"
    outside = tmp_path / "elsewhere.txt"

    runtime = Runtime(tmp_path / "abs.db")
    file_runtime(runtime, root)

    await runtime.submit_event(
        do_action(backend="local_file", target=str(outside), parameters={"content": "no"})
    )

    assert runtime.get_action_proposals()[0].status is ActionProposalStatus.FAILED
    assert not outside.exists()
    runtime.close()
