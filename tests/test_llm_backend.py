"""ExecutionBackend / FakeLLMBackend behaviour (network-free)."""

from __future__ import annotations

from nexus_seed.backends import (
    BackendRequest,
    ExecutionBackend,
    FakeLLMBackend,
    failure_response,
    invalid_response,
    proposal_response,
)


async def test_fake_backend_replays_script():
    backend = FakeLLMBackend(
        script=[failure_response("timeout"), proposal_response({"subject": "D1_CD"})]
    )
    r1 = await backend.execute(BackendRequest(instruction="go"))
    r2 = await backend.execute(BackendRequest(instruction="go"))
    r3 = await backend.execute(BackendRequest(instruction="go"))  # repeats last

    assert r1.success is False and r1.error == "timeout"
    assert r2.success is True and r2.parsed_output["subject"] == "D1_CD"
    assert r3.success is True  # last result repeats once script is exhausted
    assert len(backend.calls) == 3


async def test_helpers_shape():
    ok = proposal_response({"subject": "X"}, model="m")
    assert ok.success and ok.model == "m" and ok.parsed_output == {"subject": "X"}
    bad = invalid_response()
    assert bad.success is True and bad.parsed_output == {"unexpected": True}
    fail = failure_response("net")
    assert fail.success is False and fail.parsed_output is None


def test_fake_backend_satisfies_protocol():
    assert isinstance(FakeLLMBackend(), ExecutionBackend)
