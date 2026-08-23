"""Explicitly authorized, durable sources of external observations.

An ObservationSource is application-domain data, not a seventh Core primitive.
It records what the operator allowed NEXUS SEED to inspect. Adapters still only
describe what they saw and all resulting input passes through Ingress.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .core.event import utcnow
from .ingress.models import AdapterCheckpoint, IngressEnvelope


SYSTEM_SNAPSHOT = "system_snapshot"
SYSTEM_SNAPSHOT_EVENT = "system_snapshot_observed"
SYSTEM_FIELDS = frozenset({"platform", "hostname", "cpu", "memory", "data_disk"})
DEFAULT_SYSTEM_FIELDS = ("platform", "cpu", "memory", "data_disk")


@dataclass(slots=True)
class ObservationSource:
    """One operator-authorized source and its durable polling state."""

    name: str
    kind: str
    fields: tuple[str, ...]
    poll_interval_seconds: float = 60.0
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"source-{uuid.uuid4().hex[:12]}")
    last_checked_at: datetime | None = None
    last_changed_at: datetime | None = None
    last_event_id: uuid.UUID | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def adapter_id(self) -> str:
        """Stable Ingress adapter identity for this source."""
        return f"observation_source:{self.id}"

    def due(self, now: datetime) -> bool:
        """Whether this enabled source should be read at ``now``."""
        if not self.enabled:
            return False
        if self.last_checked_at is None:
            return True
        return now >= self.last_checked_at + timedelta(seconds=self.poll_interval_seconds)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-safe Cockpit representation."""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "fields": list(self.fields),
            "poll_interval_seconds": self.poll_interval_seconds,
            "enabled": self.enabled,
            "config": dict(self.config),
            "adapter_id": self.adapter_id,
            "last_checked_at": _iso(self.last_checked_at),
            "last_changed_at": _iso(self.last_changed_at),
            "last_event_id": str(self.last_event_id) if self.last_event_id else None,
            "last_error": self.last_error,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class SystemSnapshotAdapter:
    """Read only the fixed system fields explicitly enabled on one source."""

    source_type = SYSTEM_SNAPSHOT

    def __init__(self, source: ObservationSource, *, data_root: Path) -> None:
        if source.kind != SYSTEM_SNAPSHOT:
            raise ValueError(f"unsupported observation source kind: {source.kind}")
        invalid = set(source.fields) - SYSTEM_FIELDS
        if invalid:
            raise ValueError(f"unsupported system fields: {', '.join(sorted(invalid))}")
        self.source = source
        self.data_root = data_root.resolve()

    @property
    def adapter_id(self) -> str:
        return self.source.adapter_id

    def snapshot(self) -> dict[str, Any]:
        """Return only the fields present in the source's allow-list."""
        values: dict[str, Any] = {}
        fields = set(self.source.fields)
        if "platform" in fields:
            values["platform"] = {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            }
        if "hostname" in fields:
            values["hostname"] = socket.gethostname()
        if "cpu" in fields:
            values["cpu"] = {"logical_count": os.cpu_count()}
        if "memory" in fields:
            values["memory"] = {"total_bytes": _physical_memory_bytes()}
        if "data_disk" in fields:
            usage = shutil.disk_usage(self.data_root)
            values["data_disk"] = {
                "scope": "NEXUS_SEED_DATA_DIR",
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
            }
        return values

    def envelope(
        self, values: dict[str, Any], *, generation: int, digest: str
    ) -> IngressEnvelope:
        """Describe one changed snapshot for the existing Ingress boundary."""
        return IngressEnvelope(
            adapter_id=self.adapter_id,
            source_type=self.source_type,
            source_event_key=f"{self.source.id}:{generation}:{digest}",
            event_type=SYSTEM_SNAPSHOT_EVENT,
            payload={"source_id": self.source.id, "values": values},
            source_cursor=str(generation),
            metadata={"fields": list(self.source.fields), "digest": digest},
        )


class ObservationSourceService:
    """Manage and poll durable ObservationSources."""

    def __init__(self, runtime, *, data_root: str | Path) -> None:
        self.runtime = runtime
        self.store = runtime.observation_source_store
        self.data_root = Path(data_root).resolve()

    def create_system_snapshot(
        self,
        *,
        name: str,
        fields: list[str] | tuple[str, ...],
        poll_interval_seconds: float = 60.0,
    ) -> ObservationSource:
        """Persist an explicit system-field allow-list; no source is implicit."""
        label = (name or "").strip()
        selected = tuple(dict.fromkeys(str(item).strip() for item in fields if str(item).strip()))
        if not label:
            raise ValueError("name must not be empty")
        if not selected:
            raise ValueError("at least one observation field is required")
        invalid = set(selected) - SYSTEM_FIELDS
        if invalid:
            raise ValueError(f"unsupported system fields: {', '.join(sorted(invalid))}")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        source = ObservationSource(
            name=label,
            kind=SYSTEM_SNAPSHOT,
            fields=selected,
            poll_interval_seconds=float(poll_interval_seconds),
        )
        self.store.save(source)
        return source

    def all(self) -> list[ObservationSource]:
        """Return every configured source."""
        return self.store.all()

    def set_enabled(self, source_id: str, enabled: bool) -> ObservationSource | None:
        """Enable or disable a source without deleting its history."""
        source = self.store.get(source_id)
        if source is None:
            return None
        source.enabled = bool(enabled)
        source.updated_at = utcnow()
        self.store.save(source)
        return source

    async def poll_due(self, *, force_source_id: str | None = None) -> list[dict[str, Any]]:
        """Poll due sources once; an unchanged snapshot produces no Event."""
        now = utcnow()
        outcomes: list[dict[str, Any]] = []
        sources = self.store.all()
        if force_source_id is not None:
            sources = [item for item in sources if item.id == force_source_id]
        for source in sources:
            if not source.enabled or (force_source_id is None and not source.due(now)):
                continue
            try:
                outcome = await self._poll_system_snapshot(source, now=now)
            except Exception as exc:  # noqa: BLE001 - one source cannot stop the rest
                source.last_checked_at = now
                source.last_error = str(exc)
                source.updated_at = now
                self.store.save(source)
                outcomes.append({"source_id": source.id, "status": "ERROR", "error": str(exc)})
            else:
                outcomes.append(outcome)
        return outcomes

    async def _poll_system_snapshot(
        self, source: ObservationSource, *, now: datetime
    ) -> dict[str, Any]:
        adapter = SystemSnapshotAdapter(source, data_root=self.data_root)
        values = adapter.snapshot()
        digest = hashlib.sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        checkpoint = self.runtime.ingress.get_checkpoint(adapter.adapter_id, "snapshot")
        previous = checkpoint.cursor if checkpoint else None
        generation = int((checkpoint.metadata if checkpoint else {}).get("generation") or 0)
        source.last_checked_at = now
        source.last_error = None
        source.updated_at = now
        if previous == digest:
            with self.runtime.db.atomic():
                self.runtime.ingress.save_checkpoint(
                    AdapterCheckpoint(
                        adapter_id=adapter.adapter_id,
                        stream_key="snapshot",
                        cursor=digest,
                        metadata={"generation": generation, "checked_at": now.isoformat()},
                    )
                )
                self.store.save(source)
            return {"source_id": source.id, "status": "UNCHANGED"}

        generation += 1
        checkpoint = AdapterCheckpoint(
            adapter_id=adapter.adapter_id,
            stream_key="snapshot",
            cursor=digest,
            metadata={"generation": generation, "checked_at": now.isoformat()},
        )
        with self.runtime.db.atomic():
            ingress_result = await self.runtime.ingress.ingest(
                adapter.envelope(values, generation=generation, digest=digest),
                checkpoint=checkpoint,
                deliver=False,
            )
            source.last_changed_at = now
            source.last_event_id = ingress_result.event.id if ingress_result.event else None
            self.store.save(source)
        if ingress_result.event is not None:
            await self.runtime.deliver_event(ingress_result.event)
        return {
            "source_id": source.id,
            "status": ingress_result.status.value,
            "event_id": str(source.last_event_id) if source.last_event_id else None,
        }


def flatten_snapshot(values: dict[str, Any]) -> list[tuple[str, Any]]:
    """Flatten a snapshot into deterministic World View attributes."""
    flattened: list[tuple[str, Any]] = []
    for field_name in sorted(values):
        value = values[field_name]
        if isinstance(value, dict):
            flattened.extend(
                (f"{field_name}.{key}", value[key]) for key in sorted(value)
            )
        else:
            flattened.append((field_name, value))
    return flattened


def _physical_memory_bytes() -> int | None:
    try:
        return int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


__all__ = [
    "DEFAULT_SYSTEM_FIELDS",
    "ObservationSource",
    "ObservationSourceService",
    "SYSTEM_FIELDS",
    "SYSTEM_SNAPSHOT",
    "SYSTEM_SNAPSHOT_EVENT",
    "SystemSnapshotAdapter",
    "flatten_snapshot",
]
