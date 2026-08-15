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

import asyncio
import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from json_repair import repair_json

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
        max_tokens: int = 1024,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be greater than zero")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.model = model
        normalized_provider = provider.strip().lower().replace("-", "_")
        self.provider = (
            "openai_compatible" if normalized_provider == "openai" else normalized_provider
        )
        self.api_key_env = api_key_env
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/") if base_url else None
        self.timeout_seconds = timeout_seconds

    async def execute(self, request: BackendRequest) -> BackendResult:  # pragma: no cover - needs network
        started = time.monotonic()
        api_key = os.environ.get(self.api_key_env, "").strip()
        if self.provider == "anthropic" and not api_key:
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
        if self.provider == "openai_compatible":
            return await self._call_openai_compatible(request, api_key)
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
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in message.content if getattr(block, "type", None) == "text")
        return _parse_json_output(text)

    async def _call_openai_compatible(
        self, request: BackendRequest, api_key: str
    ) -> Any:
        """Call a local OpenAI-compatible Chat Completions endpoint."""

        if not self.base_url:
            raise RuntimeError("base_url is required for provider 'openai_compatible'")
        prompt = (
            f"{request.instruction}\n\n"
            f"Return ONLY JSON matching this schema:\n{json.dumps(request.output_schema)}\n\n"
            f"Context:\n{json.dumps(request.context, default=str)}"
        )
        response = await asyncio.to_thread(
            _post_json,
            f"{self.base_url}/chat/completions",
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self.max_tokens,
                "temperature": 0,
            },
            api_key,
            self.timeout_seconds,
        )
        return _parse_json_output(_openai_message_text(response))


def _post_json(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """POST JSON with the standard library so local LLMs need no SDK."""

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - configured HTTP endpoint
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"LLM endpoint returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"cannot reach LLM endpoint: {exc.reason}") from exc

    decoded = json.loads(body)
    if not isinstance(decoded, dict):
        raise ValueError("LLM endpoint response must be a JSON object")
    return decoded


def _openai_message_text(response: dict[str, Any]) -> str:
    """Extract assistant text from an OpenAI-compatible response."""

    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LLM endpoint response has no choices")
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if parts:
            return "".join(parts)
    raise ValueError("LLM endpoint response has no assistant message content")


def _parse_json_output(text: str) -> Any:
    """Parse model JSON, repairing malformed output only after strict parsing fails."""

    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as original:
        try:
            repaired = repair_json(
                stripped,
                ensure_ascii=False,
                skip_json_loads=True,
            )
            return json.loads(repaired)
        except (TypeError, ValueError):
            pass

        # Preserve the legacy last-resort extraction path if repair cannot
        # produce valid JSON from prose wrapped around an otherwise valid value.
        decoder = json.JSONDecoder()
        for index, character in enumerate(stripped):
            if character not in "[{":
                continue
            try:
                value, _end = decoder.raw_decode(stripped[index:])
            except json.JSONDecodeError:
                continue
            return value
        raise original
