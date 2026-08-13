"""Action backends in isolation (spec §4, §37, §38).

A backend is the narrowest thing in the system: request in, effect out.  These
tests pin that narrowness — capabilities are declared, results are data, and no
backend ever reaches for state, policy or retries.
"""

from __future__ import annotations

from nexus_seed.backends import (
    ActionRequest,
    FakeActionBackend,
    LocalFileActionBackend,
    action_failure,
    action_success,
    action_timeout,
)
from nexus_seed.backends.action import JOURNAL_NAME, summarize


# --- FakeActionBackend -----------------------------------------------------


async def test_fake_backend_replays_its_script():
    backend = FakeActionBackend(script=[action_timeout(), action_success({"n": 1})])
    first = await backend.execute(ActionRequest("write_file", "a.txt", idempotency_key="k"))
    assert not first.success and first.retryable

    second = await backend.execute(ActionRequest("write_file", "a.txt", idempotency_key="k"))
    assert second.success and second.output == {"n": 1}
    assert backend.effect_count == 1


async def test_fake_backend_repeats_the_last_scripted_result():
    backend = FakeActionBackend(script=[action_failure("down")])
    for _ in range(3):
        result = await backend.execute(ActionRequest("noop", idempotency_key="k"))
        assert not result.success
    assert len(backend.calls) == 3


async def test_fake_backend_detects_duplicate_calls():
    backend = FakeActionBackend(script=[action_success({"n": 1})])
    request = ActionRequest("write_file", "a.txt", idempotency_key="same")
    first = await backend.execute(request)
    second = await backend.execute(request)
    assert not first.duplicate
    assert second.duplicate and second.output == {"n": 1}
    assert backend.effect_count == 1


async def test_fake_backend_publishes_capabilities():
    capabilities = FakeActionBackend().capabilities()
    assert capabilities.supports("write_file")
    assert capabilities.get("write_file").required_permissions == ("filesystem.write",)
    assert capabilities.get("noop").required_permissions == ()


# --- LocalFileActionBackend ------------------------------------------------


async def test_local_file_backend_writes_and_reads(tmp_path):
    backend = LocalFileActionBackend(tmp_path / "root")

    written = await backend.execute(
        ActionRequest(
            "write_file",
            "report.txt",
            {"content": "hello"},
            idempotency_key="w1",
        )
    )
    assert written.success
    assert written.output["bytes_written"] == 5

    read = await backend.execute(
        ActionRequest("read_file", "report.txt", idempotency_key="r1")
    )
    assert read.output["content"] == "hello"


async def test_local_file_backend_creates_intermediate_directories(tmp_path):
    root = tmp_path / "root"
    backend = LocalFileActionBackend(root)
    result = await backend.execute(
        ActionRequest("write_file", "a/b/c.txt", {"content": "deep"}, idempotency_key="k")
    )
    assert result.success
    assert (root / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "deep"


async def test_local_file_backend_refuses_unknown_action_permanently(tmp_path):
    backend = LocalFileActionBackend(tmp_path / "root")
    result = await backend.execute(ActionRequest("delete_tree", "x"))
    assert not result.success
    assert not result.retryable


async def test_local_file_backend_refuses_to_overwrite_its_own_journal(tmp_path):
    backend = LocalFileActionBackend(tmp_path / "root")
    result = await backend.execute(
        ActionRequest("write_file", JOURNAL_NAME, {"content": "{}"}, idempotency_key="k")
    )
    assert not result.success and not result.retryable


async def test_local_file_backend_requires_a_target(tmp_path):
    backend = LocalFileActionBackend(tmp_path / "root")
    result = await backend.execute(ActionRequest("write_file", None, {"content": "x"}))
    assert not result.success and not result.retryable


# --- result summarisation --------------------------------------------------


def test_summarize_keeps_scalars_and_truncates_the_rest():
    summary = summarize({"n": 3, "ok": True, "path": "x" * 400, "none": None})
    assert summary["n"] == 3 and summary["ok"] is True and summary["none"] is None
    assert len(summary["path"]) == 200


def test_summarize_handles_non_dict_output():
    assert summarize(42) == {"value": "42"}
