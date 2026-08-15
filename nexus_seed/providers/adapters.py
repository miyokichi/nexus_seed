"""Provider protocol adapters; product-specific protocols stay outside Runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from .models import DelegationRequest, DelegationResult


class ProviderUnavailableBeforeStart(RuntimeError):
    """The provider refused before starting any external execution."""


@runtime_checkable
class ProviderAdapter(Protocol):
    async def execute(self, request: DelegationRequest) -> DelegationResult:
        """Execute or accept one idempotent structured delegation."""


class GenericExternalAgentAdapter:
    """Generic adapter around an async transport supplied by an application."""

    def __init__(
        self, execute: Callable[[DelegationRequest], Awaitable[DelegationResult]]
    ) -> None:
        self._execute = execute

    async def execute(self, request: DelegationRequest) -> DelegationResult:
        return await self._execute(request)


class FakeExternalAgentAdapter:
    """Deterministic external agent used by provider acceptance tests."""

    def __init__(self, results: list[DelegationResult] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[DelegationRequest] = []

    async def execute(self, request: DelegationRequest) -> DelegationResult:
        self.calls.append(request)
        if not self.results:
            raise ProviderUnavailableBeforeStart("no fake provider result configured")
        index = min(len(self.calls) - 1, len(self.results) - 1)
        result = self.results[index]
        result.invocation_id = request.invocation_id
        return result


class SkillProviderAdapter(GenericExternalAgentAdapter):
    """A skill transport with the same structured delegation contract."""
