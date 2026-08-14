"""AT1 + AT2 (spec §57, §58): the registry is a table, not a dict.

What the system can do changes while it runs — definitions register,
capabilities are disabled, versions arrive.  Holding that in memory would mean
a restart could silently answer differently (spec §12).
"""

from __future__ import annotations

import uuid

from nexus_seed.capabilities.models import Capability, CapabilityMatchStatus, CapabilityWorkMatch
from nexus_seed.storage.capability_store import CapabilityStore
from nexus_seed.storage.database import Database


def store(tmp_path, name="c.db"):
    db = Database(tmp_path / name)
    return db, CapabilityStore(db)


def test_a_capability_round_trips(tmp_path):
    """AT1."""
    db, s = store(tmp_path)
    s.save(
        Capability(
            name="analyze_resistance",
            version="1",
            description="work out the electrical consequence of a change",
            input_types=["entity"],
            output_types=["analysis"],
            tags=["electrical"],
            metadata={"owner": "process-lab"},
        )
    )

    loaded = s.get("analyze_resistance", "1")
    assert loaded.description.startswith("work out")
    assert loaded.input_types == ["entity"]
    assert loaded.output_types == ["analysis"]
    assert loaded.tags == ["electrical"]
    assert loaded.metadata == {"owner": "process-lab"}
    assert loaded.enabled is True
    db.close()


def test_re_declaring_a_capability_keeps_its_identity(tmp_path):
    """Relations point at the id, so it must not change under them."""
    db, s = store(tmp_path)
    first = s.save(Capability(name="analyze_resistance", description="v1 text"))
    second = s.save(Capability(name="analyze_resistance", description="better text"))

    assert second.id == first.id
    assert s.get("analyze_resistance", "1").description == "better text"
    assert len(s.all()) == 1
    db.close()


def test_versions_coexist(tmp_path):
    db, s = store(tmp_path)
    s.save(Capability(name="analyze_resistance", version="1"))
    s.save(Capability(name="analyze_resistance", version="2"))

    assert len(s.versions_of("analyze_resistance")) == 2
    assert {c.version for c in s.all()} == {"1", "2"}
    db.close()


def test_disabling_hides_a_capability_without_deleting_it(tmp_path):
    """Spec §42: removal is a flag, so the audit history survives."""
    db, s = store(tmp_path)
    s.save(Capability(name="analyze_resistance"))

    s.set_enabled("analyze_resistance", "1", False)

    assert s.versions_of("analyze_resistance") == []
    assert s.versions_of("analyze_resistance", enabled_only=False) != []
    assert s.get("analyze_resistance", "1").enabled is False
    db.close()


def test_a_definition_declares_what_it_provides(tmp_path):
    """AT2."""
    db, s = store(tmp_path)
    capability = s.save(Capability(name="analyze_resistance"))

    assert s.link("resistance_analyzer", "1", capability.id) is True
    # Declaring the same thing again is not new.
    assert s.link("resistance_analyzer", "1", capability.id) is False

    provided = s.capabilities_for("resistance_analyzer", "1")
    assert [c.name for c in provided] == ["analyze_resistance"]
    assert s.providers_of("analyze_resistance") == [("resistance_analyzer", "1")]
    db.close()


def test_providers_can_be_narrowed_by_version(tmp_path):
    db, s = store(tmp_path)
    v1 = s.save(Capability(name="analyze_resistance", version="1"))
    v2 = s.save(Capability(name="analyze_resistance", version="2"))
    s.link("old_analyzer", "1", v1.id)
    s.link("new_analyzer", "1", v2.id)

    assert s.providers_of("analyze_resistance", "2") == [("new_analyzer", "1")]
    assert len(s.providers_of("analyze_resistance")) == 2
    db.close()


def test_a_disabled_capability_has_no_providers(tmp_path):
    db, s = store(tmp_path)
    capability = s.save(Capability(name="analyze_resistance"))
    s.link("resistance_analyzer", "1", capability.id)

    s.set_enabled("analyze_resistance", "1", False)

    assert s.providers_of("analyze_resistance") == []
    # The relation itself is untouched — only visibility changed.
    assert len(s.all_links()) == 1
    db.close()


def test_unlinking_clears_only_that_definition(tmp_path):
    db, s = store(tmp_path)
    capability = s.save(Capability(name="analyze_resistance"))
    s.link("a", "1", capability.id)
    s.link("b", "1", capability.id)

    s.unlink_all("a", "1")

    assert s.providers_of("analyze_resistance") == [("b", "1")]
    db.close()


def test_match_attempts_accumulate_rather_than_overwrite(tmp_path):
    """Spec §48: a requirement blocked then matched keeps both attempts."""
    db, s = store(tmp_path)
    work_id = uuid.uuid4()

    s.save_match(
        CapabilityWorkMatch(
            work_requirement_id=work_id,
            status=CapabilityMatchStatus.MISSING_CAPABILITY,
            missing_capabilities=["analyze_resistance"],
            reasons=["nothing provides it"],
        )
    )
    s.save_match(
        CapabilityWorkMatch(
            work_requirement_id=work_id,
            status=CapabilityMatchStatus.MATCHED_SINGLE_PROCESS,
            selected_definition_name="resistance_analyzer",
            selected_definition_version="1",
        )
    )

    attempts = s.matches_for(work_id)
    assert [a.status for a in attempts] == [
        CapabilityMatchStatus.MISSING_CAPABILITY,
        CapabilityMatchStatus.MATCHED_SINGLE_PROCESS,
    ]
    assert attempts[0].missing_capabilities == ["analyze_resistance"]
    assert attempts[1].selected_definition_name == "resistance_analyzer"
    assert len(s.all_matches()) == 2
    db.close()
