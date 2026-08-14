"""ExecutionBackend / FakeLLMBackend behaviour (network-free)."""

from __future__ import annotations

from nexus_seed.backends import (
    BackendRequest,
    ExecutionBackend,
    FakeLLMBackend,
    LLMBackend,
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


async def test_openai_compatible_backend_calls_chat_completions_without_key(monkeypatch):
    import nexus_seed.backends.llm as llm_module

    captured = {}

    def fake_post(url, payload, api_key, timeout_seconds):
        captured.update(
            url=url,
            payload=payload,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        return {
            "choices": [
                {"message": {"content": '```json\n{"subject": "local"}\n```'}}
            ]
        }

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(llm_module, "_post_json", fake_post)
    backend = LLMBackend(
        provider="openai-compatible",
        base_url="http://127.0.0.1:1234/v1/",
        model="local-model",
        api_key_env="OPENAI_API_KEY",
        max_tokens=2048,
        timeout_seconds=30,
    )

    result = await backend.execute(
        BackendRequest(instruction="return a subject", output_schema={"type": "object"})
    )

    assert result.success is True
    assert result.parsed_output == {"subject": "local"}
    assert captured["url"] == "http://127.0.0.1:1234/v1/chat/completions"
    assert captured["payload"]["model"] == "local-model"
    assert captured["payload"]["max_tokens"] == 2048
    assert captured["api_key"] == ""
    assert captured["timeout_seconds"] == 30
