"""External adapters — the things that watch the world, and nothing else.

An adapter's whole job is::

    read the source -> decide source_event_key -> build IngressEnvelope

It must not update World State, create Observations, StateDeltas or
WorkRequirements, spawn Processes, call an LLM or perform Actions (spec §7 /
Invariants 29 and 34).  Everything an adapter produces is a *description* of
what it saw; deciding what that means happens later, inside NEXUS SEED, where
it is auditable.

Pull and push are kept as two shapes rather than forced into one (spec §8): a
webhook is handed its data and cannot be polled, a directory can be polled and
never pushes.  Pretending otherwise would buy a uniform interface nobody uses.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..ingress.models import IngressEnvelope


@runtime_checkable
class ExternalAdapter(Protocol):
    """Common shape: anything that can name itself to the ingress boundary."""

    @property
    def adapter_id(self) -> str:
        """Stable identity of this adapter, used as half the dedup key."""
        ...

    @property
    def source_type(self) -> str:
        """The kind of source this adapter reads (``"file"``, ``"webhook"``…)."""
        ...


@runtime_checkable
class PullAdapter(ExternalAdapter, Protocol):
    """An adapter NEXUS SEED asks: "what has happened since I last looked?"."""

    async def poll(self) -> list[IngressEnvelope]:
        """Return envelopes for everything observed since the last checkpoint."""
        ...


@runtime_checkable
class PushAdapter(ExternalAdapter, Protocol):
    """An adapter the outside world hands data to."""

    def envelope(self, payload: dict) -> IngressEnvelope:
        """Turn one delivered request into an envelope."""
        ...


class AdapterError(Exception):
    """An adapter could not read its source.

    Raised rather than retried: adapters never retry on their own (spec §45).
    A push source will redeliver, a pull source will be polled again, and
    ingress deduplication makes both safe.
    """
