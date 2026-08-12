"""LLM backends: a real (provider-backed) one and a deterministic fake.

* :class:`FakeLLMBackend` — the test workhorse.  It returns scripted
  :class:`BackendResult` objects (high/medium/low-confidence proposals, invalid
  output, or failures) so the whole pipeline is testable with no network.
* :class:`LLMBackend` — a thin real backend that reads its API key from the
  environment and asks the provider for structured JSON.  It is never exercised
  in tests (no network is required); it exists to show the interface is
  provider-swappable (Invariant 20).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .base import BackendRequest, BackendResult

# --- helpers to build scripted fake responses ------------------------------


def proposal_response(parsed: dict, *, model: str = "fake-llm") -> BackendResult:
    """A successful response carrying a parsed structured proposal."""
    return BackendResult(
        raw_output=json.dumps(parsed),
        parsed_output=parsed,
        model=model,
        success=True,
    )


def invalid_response(*, model: str = "fake-llm") -> BackendResult:
    """A 'successful' call whose output does not match the proposal schema."""
    return BackendResult(
        raw_output="{ this is not valid structured output }",
        parsed_output={"unexpected": True},
        model=model,
        success=True,
    )


def failure_response(error: str, *, model: str = "fake-llm") -> BackendResult:
    """A failed call (timeout / network / provider error)."""
    return BackendResult(raw_output=None, parsed_output=None, model=model, success=False, error=error)


class FakeLLMBackend:
    """A deterministic backend that replays a script of results.

    The script is a list of :class:`BackendResult`.  Each ``execute`` returns
    the next scripted result; once the script is exhausted the last result is
    repeated (so a single ``invalid_response`` keeps failing across retries).
    """

    def __init__(
        self, script: list[BackendResult] | None = None, *, default: BackendResult | None = None
    ) -> None:
        self.script = list(script or [])
        self.default = default
        self.calls: list[BackendRequest] = []

    async def execute(self, request: BackendRequest) -> BackendResult:
        self.calls.append(request)
        if self.script:
            index = min(len(self.calls) - 1, len(self.script) - 1)
            return self.script[index]
        if self.default is not None:
            return self.default
        return failure_response("no scripted response")


class LLMBackend:
    """A minimal real LLM backend (provider-swappable, network-free in tests).

    Reads the API key from ``api_key_env``.  ``execute`` lazily imports the
    provider SDK and requests JSON structured output; any problem (missing key,
    missing SDK, provider/parse error) becomes ``BackendResult(success=False)``
    so callers can apply the normal retry policy — the backend never retries on
    its own (spec §38).
    """

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-5",
        provider: str = "anthropic",
        api_key_env: str = "ANTHROPIC_API_KEY",
    ) -> None:
        self.model = model
        self.provider = provider
        self.api_key_env = api_key_env

    async def execute(self, request: BackendRequest) -> BackendResult:  # pragma: no cover - needs network
        started = time.monotonic()
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            return BackendResult(success=False, error=f"{self.api_key_env} not set", model=self.model)
        try:
            parsed = await self._call_provider(request, api_key)
        except Exception as exc:  # noqa: BLE001 - report, let caller retry
            return BackendResult(success=False, error=str(exc), model=self.model)
        latency = (time.monotonic() - started) * 1000.0
        return BackendResult(
            raw_output=parsed,
            parsed_output=parsed,
            model=self.model,
            latency_ms=latency,
            success=True,
        )

    async def _call_provider(self, request: BackendRequest, api_key: str) -> Any:  # pragma: no cover
        if self.provider != "anthropic":
            raise RuntimeError(f"unsupported provider {self.provider!r}")
        import anthropic  # lazy import; not a hard dependency

        client = anthropic.AsyncAnthropic(api_key=api_key)
        prompt = (
            f"{request.instruction}\n\n"
            f"Return ONLY JSON matching this schema:\n{json.dumps(request.output_schema)}\n\n"
            f"Context:\n{json.dumps(request.context, default=str)}"
        )
        message = await client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in message.content if getattr(block, "type", None) == "text")
        return json.loads(text)
