"""Minimal, independently usable MVP observers."""

from __future__ import annotations

from collections.abc import Iterable
import asyncio
from ..adapters.manual import ManualAdapter
from ..ingress.models import IngressEnvelope, IngressStatus
from ..ingress.service import IngressService
from .models import JsonObject, Observation


class TextObserver:
    """Expose supplied text values as one-shot manual observations.

    This is intentionally not a watcher.  Once values have been returned they
    are removed, so calling :meth:`observe` again cannot accidentally create a
    second Project for the same CLI input.
    """

    def __init__(
        self,
        values: str | Iterable[str],
        *,
        source: str = "human_input",
        metadata: JsonObject | None = None,
    ) -> None:
        self._values = [values] if isinstance(values, str) else list(values)
        self.source = source
        self.metadata = dict(metadata or {})

    def observe(self) -> list[Observation]:
        """Return every non-empty pending text value exactly once."""

        values, self._values = self._values, []
        return [
            Observation(
                content=value,
                source=self.source,
                metadata=dict(self.metadata),
            )
            for value in values
            if value.strip()
        ]


class ExistingIngressObserver:
    """Turn envelopes accepted by the existing Ingress boundary into observations.

    This adapter is the preferred application integration.  The plain
    :class:`TextObserver` remains useful when a host has already authorized and
    deduplicated input before calling the standalone MVP component.
    """

    def __init__(self, ingress: IngressService, envelopes: Iterable[IngressEnvelope]) -> None:
        self.ingress = ingress
        self._envelopes = list(envelopes)

    def observe(self) -> list[Observation]:
        """Ingest pending envelopes and expose only newly accepted occurrences."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "ExistingIngressObserver.observe is synchronous and cannot run "
                "inside an active asyncio event loop"
            )
        envelopes, self._envelopes = self._envelopes, []
        observations = []
        for envelope in envelopes:
            # Ingress records and deduplicates the occurrence.  Delivery is a
            # separate host/runtime responsibility: observing must never drain
            # existing Process work as a hidden side effect.
            result = asyncio.run(self.ingress.ingest(envelope, deliver=False))
            if result.status is not IngressStatus.ACCEPTED or result.event is None:
                continue
            event = result.event
            content = event.payload.get("content", event.payload)
            observations.append(
                Observation(
                    id=f"observation-{event.id}",
                    content=content,
                    source=envelope.source_type,
                    observed_at=event.occurred_at,
                    metadata={
                        **dict(envelope.metadata),
                        "event_id": str(event.id),
                        "ingress_receipt_id": str(event.ingress_receipt_id),
                        "source_event_key": envelope.source_event_key,
                    },
                )
            )
        return observations


class ManualIngressObserver(ExistingIngressObserver):
    """Observe one CLI/human occurrence through the existing ManualAdapter."""

    def __init__(
        self,
        ingress: IngressService,
        text: str,
        *,
        source_event_key: str,
        adapter_id: str = "mvp_manual",
        metadata: JsonObject | None = None,
    ) -> None:
        adapter = ManualAdapter(adapter_id)
        envelope = adapter.envelope(
            event_type="mvp_human_input",
            source_event_key=source_event_key,
            payload={"content": text},
            metadata=metadata,
        )
        super().__init__(ingress, [envelope])


__all__ = ["ExistingIngressObserver", "ManualIngressObserver", "TextObserver"]
