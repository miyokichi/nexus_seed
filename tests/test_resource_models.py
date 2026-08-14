"""Resource models, persistence and dedup (spec §4–§8, §11, §14, §51).

Three levels kept apart, and the two uniqueness rules that keep them honest.
"""

from __future__ import annotations

import uuid

from nexus_seed.resources.models import (
    Resource,
    ResourceRepresentation,
    ResourceVersion,
    content_hash,
    text_hash,
)
from nexus_seed.resources.service import ResourceService
from nexus_seed.storage.database import Database
from nexus_seed.storage.resource_store import ResourceStore


def store(tmp_path, name="r.db"):
    db = Database(tmp_path / name)
    return db, ResourceStore(db)


# --- hashing ---------------------------------------------------------------


def test_content_hash_is_stable_and_content_addressed():
    assert content_hash(b"abc") == content_hash(b"abc")
    assert content_hash(b"abc") != content_hash(b"abd")
    assert text_hash("abc") == content_hash(b"abc")
    assert content_hash(b"").startswith("sha256-")


# --- resource vs version ---------------------------------------------------


def test_a_resource_is_not_keyed_by_content(tmp_path):
    """Spec §5: editing a file must not fork its identity."""
    db, s = store(tmp_path)
    resource = s.save_resource(Resource(uri="file:///spec.pdf", resource_type="text"))

    for n, payload in enumerate(("AAA", "BBB", "CCC"), start=1):
        s.save_version(
            ResourceVersion(
                resource_id=resource.id, version=n, content_hash=text_hash(payload)
            )
        )

    assert len(s.all_resources()) == 1
    assert [v.version for v in s.get_versions(resource.id)] == [1, 2, 3]
    assert s.get_current_version(resource.id).version == 3
    db.close()


def test_uri_is_unique_across_resources(tmp_path):
    import sqlite3

    import pytest

    db, s = store(tmp_path)
    s.save_resource(Resource(uri="file:///a.txt"))
    with pytest.raises(sqlite3.IntegrityError):
        s.save_resource(Resource(uri="file:///a.txt"))
    db.close()


def test_versions_round_trip_with_their_provenance(tmp_path):
    db, s = store(tmp_path)
    resource = s.save_resource(Resource(uri="file:///a.txt"))
    event_id, receipt_id = uuid.uuid4(), uuid.uuid4()

    s.save_version(
        ResourceVersion(
            resource_id=resource.id,
            version=1,
            content_hash="sha256-abc",
            size_bytes=42,
            source_event_id=event_id,
            ingress_receipt_id=receipt_id,
            locator="a.txt",
            metadata={"change_type": "file_created"},
        )
    )

    loaded = s.get_current_version(resource.id)
    assert loaded.content_hash == "sha256-abc"
    assert loaded.size_bytes == 42
    assert loaded.source_event_id == event_id
    assert loaded.ingress_receipt_id == receipt_id
    assert loaded.locator == "a.txt"
    assert loaded.metadata == {"change_type": "file_created"}
    db.close()


def test_a_version_is_never_rewritten(tmp_path):
    """Spec §7: a replayed activation cannot alter history."""
    db, s = store(tmp_path)
    resource = s.save_resource(Resource(uri="file:///a.txt"))
    version = ResourceVersion(resource_id=resource.id, version=1, content_hash="sha256-A")
    s.save_version(version)

    version.content_hash = "sha256-TAMPERED"
    s.save_version(version)

    assert s.get_version(version.id).content_hash == "sha256-A"
    db.close()


def test_lookups_by_number_and_hash(tmp_path):
    db, s = store(tmp_path)
    resource = s.save_resource(Resource(uri="file:///a.txt"))
    for n, h in enumerate(("sha256-A", "sha256-B"), start=1):
        s.save_version(ResourceVersion(resource_id=resource.id, version=n, content_hash=h))

    assert s.get_version_number(resource.id, 1).content_hash == "sha256-A"
    assert s.get_version_by_hash(resource.id, "sha256-B").version == 2
    assert s.get_version_by_hash(resource.id, "sha256-Z") is None
    assert s.next_version_number(resource.id) == 3
    db.close()


def test_current_version_falls_back_to_the_highest_number(tmp_path):
    """The pointer is a projection; the versions are the truth."""
    db, s = store(tmp_path)
    resource = s.save_resource(Resource(uri="file:///a.txt"))
    s.save_version(ResourceVersion(resource_id=resource.id, version=1))
    v2 = s.save_version(ResourceVersion(resource_id=resource.id, version=2))

    s.db.execute("UPDATE resources SET current_version_id = NULL WHERE id = ?", (str(resource.id),))
    assert s.get_current_version(resource.id).id == v2.id
    db.close()


# --- representation dedup --------------------------------------------------


def test_an_identical_representation_is_not_stored_twice(tmp_path):
    """AT7 (spec §59): same version + same extractor + same type = one row."""
    db, s = store(tmp_path)
    resource = s.save_resource(Resource(uri="file:///a.txt"))
    version = s.save_version(ResourceVersion(resource_id=resource.id, version=1))

    def build():
        return ResourceRepresentation(
            resource_version_id=version.id,
            representation_type="text",
            content="hello",
            extractor_name="plain_text",
            extractor_version="1",
        )

    first = s.save_representation(build())
    second = s.save_representation(build())

    # The duplicate resolves to the original, so callers link to a real row.
    assert second.id == first.id
    assert len(s.list_representations(version.id)) == 1
    db.close()


def test_a_new_extractor_version_produces_a_new_representation(tmp_path):
    """Spec §14: improving an extractor must not rewrite past renderings."""
    db, s = store(tmp_path)
    resource = s.save_resource(Resource(uri="file:///a.txt"))
    version = s.save_version(ResourceVersion(resource_id=resource.id, version=1))

    s.save_representation(
        ResourceRepresentation(
            resource_version_id=version.id,
            representation_type="text",
            content="old rendering",
            extractor_name="plain_text",
            extractor_version="1",
        )
    )
    s.save_representation(
        ResourceRepresentation(
            resource_version_id=version.id,
            representation_type="text",
            content="better rendering",
            extractor_name="plain_text",
            extractor_version="2",
        )
    )

    assert len(s.list_representations(version.id)) == 2
    pinned = s.find_representation(
        version.id, "text", extractor_name="plain_text", extractor_version="1"
    )
    assert pinned.content == "old rendering"
    db.close()


def test_representation_identity_is_the_four_part_tuple():
    representation = ResourceRepresentation(
        resource_version_id=uuid.uuid4(),
        representation_type="text",
        extractor_name="plain_text",
        extractor_version="1",
    )
    assert representation.identity[1:] == ("text", "plain_text", "1")


# --- the indexing decision -------------------------------------------------


def test_index_observation_creates_then_versions_then_dedups(tmp_path):
    db, s = store(tmp_path)
    service = ResourceService(s)

    first = service.index_observation(
        uri="file:///a.txt", locator="a.txt", observed_hash="sha256-A"
    )
    assert first.resource_is_new and first.created_version
    assert first.version.version == 1
    s.save_resource(first.resource)
    s.save_version(first.version)

    second = service.index_observation(
        uri="file:///a.txt", locator="a.txt", observed_hash="sha256-B"
    )
    assert not second.resource_is_new and second.created_version
    assert second.version.version == 2
    s.save_resource(second.resource)
    s.save_version(second.version)

    third = service.index_observation(
        uri="file:///a.txt", locator="a.txt", observed_hash="sha256-B"
    )
    assert not third.created_version
    assert third.reason == "content_hash unchanged"
    assert third.unchanged_version.version == 2
    db.close()


def test_index_observation_reports_unreadable_content(tmp_path):
    db, s = store(tmp_path)
    service = ResourceService(s)  # no scope configured

    result = service.index_observation(uri="file:///a.txt", locator="a.txt")

    assert not result.created_version
    assert "unreadable" in result.reason
    db.close()
