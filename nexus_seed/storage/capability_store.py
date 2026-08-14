"""Persistence for capabilities, who provides them, and matching history.

The registry is a *table*, not a module-level dict (spec §12).  What the system
can do changes while it runs — definitions are registered, capabilities are
disabled, versions arrive — and a restart has to see the same answers it saw
before, or capability matching would silently change behaviour.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ..capabilities.models import (
    Capability,
    CapabilityMatchStatus,
    CapabilityWorkMatch,
)
from ..core.event import utcnow
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class CapabilityStore:
    """Stores capabilities, process→capability relations and match attempts."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- capabilities ------------------------------------------------------

    def save(self, capability: Capability) -> Capability:
        """Insert or update a capability (unique by ``name`` + ``version``).

        Returns the *stored* capability: re-declaring an existing one keeps its
        original id, so the relations pointing at it stay valid.
        """
        existing = self.get(capability.name, capability.version)
        if existing is not None:
            capability.id = existing.id
            capability.created_at = existing.created_at
        self.db.execute(
            """
            INSERT INTO capabilities
                (id, name, version, description, input_types_json, output_types_json,
                 tags_json, metadata_json, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, version) DO UPDATE SET
                description = excluded.description,
                input_types_json = excluded.input_types_json,
                output_types_json = excluded.output_types_json,
                tags_json = excluded.tags_json,
                metadata_json = excluded.metadata_json,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (
                str(capability.id),
                capability.name,
                capability.version,
                capability.description,
                dumps(capability.input_types),
                dumps(capability.output_types),
                dumps(capability.tags),
                dumps(capability.metadata),
                int(capability.enabled),
                capability.created_at.isoformat(),
                utcnow().isoformat(),
            ),
        )
        return capability

    def get(self, name: str, version: str) -> Capability | None:
        """Return one capability by its logical identity."""
        row = self.db.query_one(
            "SELECT * FROM capabilities WHERE name = ? AND version = ?", (name, version)
        )
        return self._capability(row) if row else None

    def get_by_id(self, capability_id: uuid.UUID) -> Capability | None:
        """Return a capability by id."""
        row = self.db.query_one(
            "SELECT * FROM capabilities WHERE id = ?", (str(capability_id),)
        )
        return self._capability(row) if row else None

    def versions_of(self, name: str, *, enabled_only: bool = True) -> list[Capability]:
        """Return every version of ``name``, newest declaration first."""
        sql = "SELECT * FROM capabilities WHERE name = ?"
        params: list = [name]
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY version DESC"
        return [self._capability(r) for r in self.db.query(sql, tuple(params))]

    def all(self, *, enabled_only: bool = False) -> list[Capability]:
        """Return every capability, by name then version."""
        sql = "SELECT * FROM capabilities"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name ASC, version ASC"
        return [self._capability(r) for r in self.db.query(sql)]

    def set_enabled(self, name: str, version: str, enabled: bool) -> None:
        """Enable or disable a capability (never a delete — spec §42)."""
        self.db.execute(
            "UPDATE capabilities SET enabled = ?, updated_at = ? "
            "WHERE name = ? AND version = ?",
            (int(enabled), utcnow().isoformat(), name, version),
        )

    # --- process relations -------------------------------------------------

    def link(
        self,
        definition_name: str,
        definition_version: str,
        capability_id: uuid.UUID,
        metadata: dict | None = None,
    ) -> bool:
        """Record that a definition provides a capability.

        Returns whether this was *new* — the caller uses that to decide whether
        anything genuinely became available (spec §83), so re-registering the
        same definition does not announce availability again.
        """
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO process_capabilities
                (id, definition_name, definition_version, capability_id,
                 metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                definition_name,
                definition_version,
                str(capability_id),
                dumps(metadata or {}),
                utcnow().isoformat(),
            ),
        )
        return cursor.rowcount > 0

    def unlink_all(self, definition_name: str, definition_version: str) -> None:
        """Drop a definition's declarations (before re-declaring them)."""
        self.db.execute(
            "DELETE FROM process_capabilities WHERE definition_name = ? "
            "AND definition_version = ?",
            (definition_name, definition_version),
        )

    def capabilities_for(
        self, definition_name: str, definition_version: str
    ) -> list[Capability]:
        """Return the capabilities a definition declares."""
        rows = self.db.query(
            """
            SELECT c.* FROM capabilities c
            JOIN process_capabilities pc ON pc.capability_id = c.id
            WHERE pc.definition_name = ? AND pc.definition_version = ?
            ORDER BY c.name ASC, c.version ASC
            """,
            (definition_name, definition_version),
        )
        return [self._capability(r) for r in rows]

    def providers_of(
        self, name: str, version: str | None = None, *, enabled_only: bool = True
    ) -> list[tuple[str, str]]:
        """Return ``(definition_name, definition_version)`` providing a capability."""
        sql = """
            SELECT pc.definition_name, pc.definition_version FROM process_capabilities pc
            JOIN capabilities c ON c.id = pc.capability_id
            WHERE c.name = ?
        """
        params: list = [name]
        if version is not None:
            sql += " AND c.version = ?"
            params.append(version)
        if enabled_only:
            sql += " AND c.enabled = 1"
        sql += " ORDER BY pc.definition_name ASC, pc.definition_version ASC"
        return [
            (r["definition_name"], r["definition_version"])
            for r in self.db.query(sql, tuple(params))
        ]

    def all_links(self) -> list[tuple[str, str, uuid.UUID]]:
        """Return every ``(definition_name, definition_version, capability_id)``."""
        rows = self.db.query(
            "SELECT * FROM process_capabilities ORDER BY created_at ASC"
        )
        return [
            (r["definition_name"], r["definition_version"], uuid.UUID(r["capability_id"]))
            for r in rows
        ]

    # --- match audit -------------------------------------------------------

    def save_match(self, match: CapabilityWorkMatch) -> CapabilityWorkMatch:
        """Append a matching attempt (never overwrites an earlier one)."""
        self.db.execute(
            """
            INSERT INTO capability_work_matches
                (id, work_requirement_id, status, required_capabilities_json,
                 candidates_json, missing_capabilities_json, selected_definition_name,
                 selected_definition_version, reasons_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(match.id),
                str(match.work_requirement_id),
                match.status.value,
                dumps(match.required_capabilities),
                dumps(match.candidates),
                dumps(match.missing_capabilities),
                match.selected_definition_name,
                match.selected_definition_version,
                dumps(match.reasons),
                match.created_at.isoformat(),
            ),
        )
        return match

    def matches_for(self, work_requirement_id: uuid.UUID) -> list[CapabilityWorkMatch]:
        """Return every matching attempt for a requirement, oldest first."""
        rows = self.db.query(
            "SELECT * FROM capability_work_matches WHERE work_requirement_id = ? "
            "ORDER BY created_at ASC",
            (str(work_requirement_id),),
        )
        return [self._match(r) for r in rows]

    def all_matches(self) -> list[CapabilityWorkMatch]:
        """Return every matching attempt, oldest first."""
        rows = self.db.query(
            "SELECT * FROM capability_work_matches ORDER BY created_at ASC"
        )
        return [self._match(r) for r in rows]

    # --- row mapping -------------------------------------------------------

    @staticmethod
    def _capability(row) -> Capability:
        return Capability(
            name=row["name"],
            version=row["version"],
            description=row["description"],
            input_types=loads(row["input_types_json"]) or [],
            output_types=loads(row["output_types_json"]) or [],
            tags=loads(row["tags_json"]) or [],
            metadata=loads(row["metadata_json"]) or {},
            enabled=bool(row["enabled"]),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _match(row) -> CapabilityWorkMatch:
        return CapabilityWorkMatch(
            work_requirement_id=uuid.UUID(row["work_requirement_id"]),
            status=CapabilityMatchStatus(row["status"]),
            required_capabilities=loads(row["required_capabilities_json"]) or [],
            candidates=loads(row["candidates_json"]) or [],
            missing_capabilities=loads(row["missing_capabilities_json"]) or [],
            selected_definition_name=row["selected_definition_name"],
            selected_definition_version=row["selected_definition_version"],
            reasons=loads(row["reasons_json"]) or [],
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
