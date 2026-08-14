"""Persistence for Resources, their versions and their representations.

Three tables, one store, because the three are never queried apart: a version
is meaningless without its resource, and a representation without its version.

Two uniqueness constraints do real work:

* ``resources.uri`` — one Resource per thing in the world, so editing a file
  appends a version instead of forking its history;
* ``(resource_version_id, representation_type, extractor_name,
  extractor_version)`` — re-running an extractor cannot produce a second copy.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from ..core.event import utcnow
from ..resources.models import Resource, ResourceRepresentation, ResourceVersion
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class ResourceStore:
    """Reads and writes Resources, ResourceVersions and Representations."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- resources ---------------------------------------------------------

    def save_resource(self, resource: Resource) -> Resource:
        """Insert or update a Resource (idempotent by id, unique by uri)."""
        self.db.execute(
            """
            INSERT INTO resources
                (id, uri, resource_type, source_adapter_id, source_identity,
                 current_version_id, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                resource_type = excluded.resource_type,
                source_adapter_id = excluded.source_adapter_id,
                source_identity = excluded.source_identity,
                current_version_id = excluded.current_version_id,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                str(resource.id),
                resource.uri,
                resource.resource_type,
                resource.source_adapter_id,
                resource.source_identity,
                str(resource.current_version_id) if resource.current_version_id else None,
                dumps(resource.metadata),
                resource.created_at.isoformat(),
                utcnow().isoformat(),
            ),
        )
        return resource

    def get_resource(self, resource_id: uuid.UUID) -> Resource | None:
        """Return the Resource with ``resource_id``, or ``None``."""
        row = self.db.query_one("SELECT * FROM resources WHERE id = ?", (str(resource_id),))
        return self._resource(row) if row else None

    def get_resource_by_uri(self, uri: str) -> Resource | None:
        """Return the Resource identified by ``uri``, or ``None``."""
        row = self.db.query_one("SELECT * FROM resources WHERE uri = ?", (uri,))
        return self._resource(row) if row else None

    def all_resources(self) -> list[Resource]:
        """Return every Resource, oldest first."""
        rows = self.db.query("SELECT * FROM resources ORDER BY created_at ASC")
        return [self._resource(r) for r in rows]

    def set_current_version(self, resource_id: uuid.UUID, version_id: uuid.UUID) -> None:
        """Advance a Resource's current-version pointer (a projection)."""
        self.db.execute(
            "UPDATE resources SET current_version_id = ?, updated_at = ? WHERE id = ?",
            (str(version_id), utcnow().isoformat(), str(resource_id)),
        )

    # --- versions ----------------------------------------------------------

    def save_version(self, version: ResourceVersion) -> ResourceVersion:
        """Insert an immutable ResourceVersion and make it current.

        Versions are never updated (spec §7); a re-insert of the same id is
        ignored so a replayed activation cannot rewrite history.
        """
        self.db.execute(
            """
            INSERT OR IGNORE INTO resource_versions
                (id, resource_id, version, content_hash, size_bytes, source_event_id,
                 ingress_receipt_id, locator, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(version.id),
                str(version.resource_id),
                version.version,
                version.content_hash,
                version.size_bytes,
                str(version.source_event_id) if version.source_event_id else None,
                str(version.ingress_receipt_id) if version.ingress_receipt_id else None,
                version.locator,
                dumps(version.metadata),
                version.created_at.isoformat(),
            ),
        )
        self.set_current_version(version.resource_id, version.id)
        return version

    def get_version(self, version_id: uuid.UUID) -> ResourceVersion | None:
        """Return the version with ``version_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM resource_versions WHERE id = ?", (str(version_id),)
        )
        return self._version(row) if row else None

    def get_version_number(self, resource_id: uuid.UUID, version: int) -> ResourceVersion | None:
        """Return a specific numbered version of a Resource."""
        row = self.db.query_one(
            "SELECT * FROM resource_versions WHERE resource_id = ? AND version = ?",
            (str(resource_id), version),
        )
        return self._version(row) if row else None

    def get_versions(self, resource_id: uuid.UUID) -> list[ResourceVersion]:
        """Return every version of a Resource, oldest first."""
        rows = self.db.query(
            "SELECT * FROM resource_versions WHERE resource_id = ? ORDER BY version ASC",
            (str(resource_id),),
        )
        return [self._version(r) for r in rows]

    def get_current_version(self, resource_id: uuid.UUID) -> ResourceVersion | None:
        """Return the newest version of a Resource.

        Reads the pointer first and falls back to the highest version number,
        so a Resource whose pointer was never set still answers correctly.
        """
        resource = self.get_resource(resource_id)
        if resource is not None and resource.current_version_id is not None:
            version = self.get_version(resource.current_version_id)
            if version is not None:
                return version
        row = self.db.query_one(
            "SELECT * FROM resource_versions WHERE resource_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (str(resource_id),),
        )
        return self._version(row) if row else None

    def get_version_by_hash(
        self, resource_id: uuid.UUID, content_hash: str
    ) -> ResourceVersion | None:
        """Return an existing version of a Resource with this content."""
        row = self.db.query_one(
            "SELECT * FROM resource_versions WHERE resource_id = ? AND content_hash = ? "
            "ORDER BY version ASC LIMIT 1",
            (str(resource_id), content_hash),
        )
        return self._version(row) if row else None

    def next_version_number(self, resource_id: uuid.UUID) -> int:
        """Return the version number a new version of this Resource would take."""
        row = self.db.query_one(
            "SELECT MAX(version) AS n FROM resource_versions WHERE resource_id = ?",
            (str(resource_id),),
        )
        return int((row["n"] or 0) + 1)

    # --- representations ---------------------------------------------------

    def save_representation(
        self, representation: ResourceRepresentation
    ) -> ResourceRepresentation:
        """Insert a Representation, ignoring an identical one (spec §14).

        Returns the *stored* representation, which for a duplicate is the one
        that was already there — so callers link to the original rather than to
        a row that was silently dropped.
        """
        try:
            self.db.execute(
                """
                INSERT INTO resource_representations
                    (id, resource_version_id, representation_type, content_json,
                     metadata_json, created_by_process_id, extractor_name,
                     extractor_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(representation.id),
                    str(representation.resource_version_id),
                    representation.representation_type,
                    dumps(representation.content),
                    dumps(representation.metadata),
                    str(representation.created_by_process_id)
                    if representation.created_by_process_id
                    else None,
                    representation.extractor_name,
                    representation.extractor_version,
                    representation.created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            existing = self.find_representation(
                representation.resource_version_id,
                representation.representation_type,
                extractor_name=representation.extractor_name,
                extractor_version=representation.extractor_version,
            )
            if existing is not None:
                return existing
            raise
        return representation

    def get_representation(self, representation_id: uuid.UUID) -> ResourceRepresentation | None:
        """Return the representation with ``representation_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM resource_representations WHERE id = ?", (str(representation_id),)
        )
        return self._representation(row) if row else None

    def find_representation(
        self,
        resource_version_id: uuid.UUID,
        representation_type: str,
        *,
        extractor_name: str | None = None,
        extractor_version: str | None = None,
    ) -> ResourceRepresentation | None:
        """Return a representation of a version by type (newest first).

        Naming the extractor narrows it to one exact logical identity; omitting
        it asks "is there *any* text rendering of this version?".
        """
        sql = (
            "SELECT * FROM resource_representations "
            "WHERE resource_version_id = ? AND representation_type = ?"
        )
        params: list = [str(resource_version_id), representation_type]
        if extractor_name is not None:
            sql += " AND extractor_name = ?"
            params.append(extractor_name)
        if extractor_version is not None:
            sql += " AND extractor_version = ?"
            params.append(extractor_version)
        sql += " ORDER BY created_at DESC LIMIT 1"
        row = self.db.query_one(sql, tuple(params))
        return self._representation(row) if row else None

    def list_representations(
        self, resource_version_id: uuid.UUID
    ) -> list[ResourceRepresentation]:
        """Return every representation of a version, oldest first."""
        rows = self.db.query(
            "SELECT * FROM resource_representations WHERE resource_version_id = ? "
            "ORDER BY created_at ASC",
            (str(resource_version_id),),
        )
        return [self._representation(r) for r in rows]

    def all_representations(self) -> list[ResourceRepresentation]:
        """Return every representation, oldest first."""
        rows = self.db.query(
            "SELECT * FROM resource_representations ORDER BY created_at ASC"
        )
        return [self._representation(r) for r in rows]

    # --- row mapping -------------------------------------------------------

    @staticmethod
    def _resource(row) -> Resource:
        return Resource(
            uri=row["uri"],
            resource_type=row["resource_type"],
            source_adapter_id=row["source_adapter_id"],
            source_identity=row["source_identity"],
            current_version_id=_uuid(row["current_version_id"]),
            metadata=loads(row["metadata_json"]) or {},
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _version(row) -> ResourceVersion:
        return ResourceVersion(
            resource_id=uuid.UUID(row["resource_id"]),
            version=row["version"],
            content_hash=row["content_hash"],
            size_bytes=row["size_bytes"],
            source_event_id=_uuid(row["source_event_id"]),
            ingress_receipt_id=_uuid(row["ingress_receipt_id"]),
            locator=row["locator"],
            metadata=loads(row["metadata_json"]) or {},
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _representation(row) -> ResourceRepresentation:
        return ResourceRepresentation(
            resource_version_id=uuid.UUID(row["resource_version_id"]),
            representation_type=row["representation_type"],
            content=loads(row["content_json"]),
            extractor_name=row["extractor_name"],
            extractor_version=row["extractor_version"],
            metadata=loads(row["metadata_json"]) or {},
            created_by_process_id=_uuid(row["created_by_process_id"]),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
