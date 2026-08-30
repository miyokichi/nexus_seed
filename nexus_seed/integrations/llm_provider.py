"""MVP LLM Provider adapters."""

from __future__ import annotations

import asyncio
from typing import cast

from ..backends.base import BackendRequest, ExecutionBackend
from ..platform.contracts.mvp import JsonObject, JsonValue


class ExistingBackendLLMProvider:
    """Adapt the existing asynchronous ExecutionBackend to MVP ``generate``.

    MVP's application service and CLI are synchronous.  The adapter therefore
    owns the small sync/async seam while the existing backend remains unchanged.
    """

    def __init__(self, backend: ExecutionBackend) -> None:
        self.backend = backend

    def generate(
        self,
        messages: list[JsonObject],
        schema: JsonObject | None = None,
    ) -> JsonValue:
        """Execute one provider-neutral request and return parsed output."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "ExistingBackendLLMProvider.generate is synchronous and cannot "
                "run inside an active asyncio event loop"
            )

        request = BackendRequest(
            instruction=_instruction(messages),
            context={"messages": messages},
            output_schema=schema,
            metadata={"surface": "mvp"},
        )
        result = asyncio.run(self.backend.execute(request))
        if not result.success:
            raise RuntimeError(result.error or "LLM backend failed")
        if result.parsed_output is not None:
            return cast(JsonValue, result.parsed_output)
        return cast(JsonValue, result.raw_output)


def _instruction(messages: list[JsonObject]) -> str:
    parts = [
        f"{message.get('role', 'user')}: {message.get('content', '')}"
        for message in messages
    ]
    return "\n".join(parts)


__all__ = ["ExistingBackendLLMProvider"]
