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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core.event import utcnow
from .ingress.models import AdapterCheckpoint, IngressEnvelope


SYSTEM_SNAPSHOT = "system_snapshot"
SYSTEM_SNAPSHOT_EVENT = "system_snapshot_observed"
SYSTEM_FIELDS = frozenset({"platform", "hostname", "cpu", "memory", "data_disk"})
DEFAULT_SYSTEM_FIELDS = ("platform", "cpu", "memory", "data_disk")

#: A folder's *standing situation*, which is a different question from the
#: file observer's "this file changed": how much is in there, how old the
#: oldest thing is, what kinds of file they are.  A backlog is a fact about
#: the world even on a day when nothing changed.
FOLDER_STATUS = "folder_status"
FOLDER_STATUS_EVENT = "folder_status_observed"
FOLDER_FIELDS = frozenset(
    {"file_count", "total_bytes", "oldest_change", "newest_change", "by_extension"}
)
DEFAULT_FOLDER_FIELDS = ("file_count", "total_bytes", "oldest_change")

#: How many entries one folder poll will stat.  A source is a standing
#: observation, not a crawl: past this the count is reported as capped rather
#: than the poll growing without bound.
FOLDER_SCAN_LIMIT = 5000


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


class FolderStatusAdapter:
    """Read how much is sitting in one operator-authorized folder.

    Only directory metadata is read — name, size and modification time from
    ``stat``.  No file is ever opened, so authorizing a folder here says
    nothing about letting NEXUS SEED read what is in it; that is the file
    observer's separate, separately-scoped job.
    """

    source_type = FOLDER_STATUS

    def __init__(self, source: ObservationSource) -> None:
        if source.kind != FOLDER_STATUS:
            raise ValueError(f"unsupported observation source kind: {source.kind}")
        invalid = set(source.fields) - FOLDER_FIELDS
        if invalid:
            raise ValueError(f"unsupported folder fields: {', '.join(sorted(invalid))}")
        path = str(source.config.get("path") or "").strip()
        if not path:
            raise ValueError("a folder observation source needs config['path']")
        self.source = source
        self.path = Path(path).expanduser().resolve()
        self.recursive = bool(source.config.get("recursive", True))

    @property
    def adapter_id(self) -> str:
        return self.source.adapter_id

    def snapshot(self) -> dict[str, Any]:
        """Return only the fields present in the source's allow-list."""
        if not self.path.is_dir():
            raise ValueError(f"not a readable folder: {self.path}")
        fields = set(self.source.fields)
        count = 0
        total = 0
        oldest: float | None = None
        newest: float | None = None
        by_extension: dict[str, int] = {}
        truncated = False

        walker = self.path.rglob("*") if self.recursive else self.path.glob("*")
        for entry in walker:
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:  # vanished or unreadable; it is simply not counted
                continue
            count += 1
            if count > FOLDER_SCAN_LIMIT:
                truncated = True
                break
            total += stat.st_size
            oldest = stat.st_mtime if oldest is None else min(oldest, stat.st_mtime)
            newest = stat.st_mtime if newest is None else max(newest, stat.st_mtime)
            suffix = entry.suffix.lower() or "(none)"
            by_extension[suffix] = by_extension.get(suffix, 0) + 1

        values: dict[str, Any] = {}
        if "file_count" in fields:
            values["file_count"] = min(count, FOLDER_SCAN_LIMIT)
            if truncated:
                values["file_count_capped"] = True
        if "total_bytes" in fields:
            values["total_bytes"] = total
        if "oldest_change" in fields:
            values["oldest_change"] = _iso_from_epoch(oldest)
        if "newest_change" in fields:
            values["newest_change"] = _iso_from_epoch(newest)
        if "by_extension" in fields:
            values["by_extension"] = dict(sorted(by_extension.items()))
        return values

    def envelope(
        self, values: dict[str, Any], *, generation: int, digest: str
    ) -> IngressEnvelope:
        """Describe one changed folder reading for the existing Ingress boundary."""
        return IngressEnvelope(
            adapter_id=self.adapter_id,
            source_type=self.source_type,
            source_event_key=f"{self.source.id}:{generation}:{digest}",
            event_type=FOLDER_STATUS_EVENT,
            payload={
                "source_id": self.source.id,
                "path": str(self.path),
                "values": values,
            },
            source_cursor=str(generation),
            metadata={
                "fields": list(self.source.fields),
                "digest": digest,
                "path": str(self.path),
            },
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

    def create_folder_status(
        self,
        *,
        name: str,
        path: str | Path,
        fields: list[str] | tuple[str, ...] = DEFAULT_FOLDER_FIELDS,
        poll_interval_seconds: float = 300.0,
        recursive: bool = True,
    ) -> ObservationSource:
        """Persist one folder the operator authorized NEXUS SEED to size up.

        Only directory metadata is read; no file is opened.  The folder must
        exist when it is registered, so a typo is refused here rather than
        becoming a source that silently fails on every poll.
        """
        label = (name or "").strip()
        selected = tuple(
            dict.fromkeys(str(item).strip() for item in fields if str(item).strip())
        )
        if not label:
            raise ValueError("name must not be empty")
        if not selected:
            raise ValueError("at least one observation field is required")
        invalid = set(selected) - FOLDER_FIELDS
        if invalid:
            raise ValueError(f"unsupported folder fields: {', '.join(sorted(invalid))}")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"not a readable folder: {resolved}")
        source = ObservationSource(
            name=label,
            kind=FOLDER_STATUS,
            fields=selected,
            poll_interval_seconds=float(poll_interval_seconds),
            config={"path": str(resolved), "recursive": bool(recursive)},
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
                outcome = await self._poll(source, now=now)
            except Exception as exc:  # noqa: BLE001 - one source cannot stop the rest
                source.last_checked_at = now
                source.last_error = str(exc)
                source.updated_at = now
                self.store.save(source)
                outcomes.append({"source_id": source.id, "status": "ERROR", "error": str(exc)})
            else:
                outcomes.append(outcome)
        return outcomes

    def _adapter(self, source: ObservationSource):
        """The reader for one source kind.  Adding a kind is adding a case."""
        if source.kind == SYSTEM_SNAPSHOT:
            return SystemSnapshotAdapter(source, data_root=self.data_root)
        if source.kind == FOLDER_STATUS:
            return FolderStatusAdapter(source)
        raise ValueError(f"unsupported observation source kind: {source.kind}")

    async def _poll(
        self, source: ObservationSource, *, now: datetime
    ) -> dict[str, Any]:
        """Read one source; an unchanged reading produces no Event.

        Kind-independent: every source is read, digested, compared with its
        checkpoint and — only when the reading actually differs — put through
        the same Ingress boundary as any other external observation.
        """
        adapter = self._adapter(source)
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


def _iso_from_epoch(value: float | None) -> str | None:
    """A filesystem timestamp as UTC ISO-8601, or ``None`` when there is none."""
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


__all__ = [
    "DEFAULT_FOLDER_FIELDS",
    "DEFAULT_SYSTEM_FIELDS",
    "FOLDER_FIELDS",
    "FOLDER_SCAN_LIMIT",
    "FOLDER_STATUS",
    "FOLDER_STATUS_EVENT",
    "FolderStatusAdapter",
    "ObservationSource",
    "ObservationSourceService",
    "SYSTEM_FIELDS",
    "SYSTEM_SNAPSHOT",
    "SYSTEM_SNAPSHOT_EVENT",
    "SystemSnapshotAdapter",
    "flatten_snapshot",
]
