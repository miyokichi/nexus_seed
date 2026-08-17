"""Skill Loader: scan roots, validate contracts, resolve precedence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_seed.providers import (
    DelegationResult,
    DelegationStatus,
    DirectorySkillAdapter,
    ExecutionProvider,
    ProviderHealth,
    ProviderKind,
    SkillLoader,
    SkillValidationError,
)
from nexus_seed.runtime.runtime import Runtime

#: The Skill packages that ship with the repository.
REPO_SKILLS = Path(__file__).resolve().parents[1] / "skills"


def skill(
    root,
    name="world_event_interpretation",
    *,
    version="1",
    manifest=None,
    instructions="# Interpret\n\nCompare the Observation with World State.",
    instruction_file="SKILL.md",
    write_instructions=True,
):
    """Write one skill package and return its directory."""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    body = {
        "name": name,
        "version": version,
        "description": f"{name} skill",
        "capabilities": [name],
        "input_ports": ["observation"],
        "output_ports": ["state_delta_candidate"],
        "required_permissions": [],
        "instruction": instruction_file,
    }
    body.update(manifest or {})
    (package / "skill.json").write_text(json.dumps(body), encoding="utf-8")
    if write_instructions:
        (package / instruction_file).write_text(instructions, encoding="utf-8")
    return package


async def transport(request):
    return DelegationResult(
        invocation_id=request.invocation_id,
        status=DelegationStatus.COMPLETED,
        typed_outputs=[{"type": "state_delta_candidate", "value": {}}],
    )


def test_valid_skill_loads_with_contract_and_instructions(tmp_path):
    skill(tmp_path / "skills")

    catalog = SkillLoader([tmp_path / "skills"]).load()

    assert catalog.failures == []
    loaded = catalog.get("world_event_interpretation")
    assert loaded.capability_names == ("world_event_interpretation",)
    assert loaded.descriptor.instructions.startswith("# Interpret")
    assert loaded.descriptor.output_ports == ["state_delta_candidate"]


def test_malformed_manifest_is_reported_without_stopping_the_load(tmp_path):
    root = tmp_path / "skills"
    skill(root, "goal_evaluation")
    broken = root / "broken"
    broken.mkdir()
    (broken / "skill.json").write_text("{not json", encoding="utf-8")
    (broken / "SKILL.md").write_text("# Broken", encoding="utf-8")

    catalog = SkillLoader([root]).load()

    assert [s.name for s in catalog.list()] == ["goal_evaluation"]
    assert len(catalog.failures) == 1
    failure = catalog.failures[0]
    assert failure.path == broken
    assert "invalid skill.json" in failure.error


def test_strict_mode_refuses_to_start_on_a_broken_package(tmp_path):
    root = tmp_path / "skills"
    broken = root / "broken"
    broken.mkdir(parents=True)
    (broken / "skill.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(SkillValidationError, match="invalid skill.json"):
        SkillLoader([root], strict=True).load()


def test_missing_instruction_file_is_a_contract_error(tmp_path):
    root = tmp_path / "skills"
    skill(root, "project_planning", write_instructions=False)

    catalog = SkillLoader([root]).load()

    assert catalog.list() == []
    assert "requires skill.json and SKILL.md" in catalog.failures[0].error


def test_named_instruction_file_is_honoured(tmp_path):
    root = tmp_path / "skills"
    skill(root, "work_generation", instruction_file="PROCEDURE.md")

    catalog = SkillLoader([root]).load()

    assert catalog.get("work_generation").descriptor.metadata["instruction_file"] == (
        "PROCEDURE.md"
    )


def test_instruction_file_cannot_escape_the_package(tmp_path):
    root = tmp_path / "skills"
    package = skill(root, "work_assignment")
    manifest = json.loads((package / "skill.json").read_text(encoding="utf-8"))
    manifest["instruction"] = "../../secrets.md"
    (package / "skill.json").write_text(json.dumps(manifest), encoding="utf-8")

    catalog = SkillLoader([root]).load()

    assert catalog.list() == []
    assert "must stay inside the skill package" in catalog.failures[0].error


def test_disabled_skill_is_not_registered(tmp_path):
    root = tmp_path / "skills"
    skill(root, "goal_evaluation", manifest={"enabled": False})

    catalog = SkillLoader([root]).load()

    assert catalog.list() == []
    assert catalog.disabled == ["goal_evaluation"]


def test_unknown_manifest_field_is_rejected(tmp_path):
    root = tmp_path / "skills"
    skill(root, "project_planning", manifest={"little_agent_skill": "python_coder"})

    catalog = SkillLoader([root]).load()

    assert catalog.list() == []
    assert "unknown fields" in catalog.failures[0].error


def test_duplicate_name_inside_one_root_is_always_an_error(tmp_path):
    root = tmp_path / "skills"
    skill(root, "first", manifest={"name": "shared"})
    skill(root, "second", manifest={"name": "shared"})

    catalog = SkillLoader([root]).load()

    assert [s.name for s in catalog.list()] == ["shared"]
    assert "duplicate skill name 'shared'" in catalog.failures[0].error


def test_first_root_wins_and_records_what_it_shadows(tmp_path):
    project = tmp_path / "project"
    user = tmp_path / "user"
    skill(project, "goal_evaluation", instructions="# Project copy")
    shadowed = skill(user, "goal_evaluation", instructions="# User copy")

    catalog = SkillLoader([project, user]).load()

    loaded = catalog.get("goal_evaluation")
    assert loaded.descriptor.instructions == "# Project copy"
    assert loaded.root == project.resolve()
    assert loaded.shadows == (shadowed,)


def test_override_can_be_configured_as_a_duplicate_error(tmp_path):
    project = tmp_path / "project"
    user = tmp_path / "user"
    skill(project, "goal_evaluation")
    skill(user, "goal_evaluation")

    catalog = SkillLoader([project, user], on_duplicate="error").load()

    assert len(catalog.list()) == 1
    assert "already loaded from" in catalog.failures[0].error


def test_multiple_roots_load_the_union_deterministically(tmp_path):
    project = tmp_path / "project"
    user = tmp_path / "user"
    skill(project, "world_event_interpretation")
    skill(project, "goal_evaluation")
    skill(user, "project_planning")

    first = SkillLoader([project, user]).load()
    second = SkillLoader([project, user]).load()

    assert [s.name for s in first.list()] == [s.name for s in second.list()]
    assert sorted(s.name for s in first.list()) == [
        "goal_evaluation",
        "project_planning",
        "world_event_interpretation",
    ]


def test_missing_root_is_not_an_error(tmp_path):
    catalog = SkillLoader([tmp_path / "absent"]).load()

    assert catalog.list() == []
    assert catalog.failures == []


def test_catalog_finds_skills_by_capability(tmp_path):
    root = tmp_path / "skills"
    skill(
        root,
        "world_event_interpretation",
        manifest={"capabilities": ["world_event_interpretation", "observation_triage"]},
    )
    skill(root, "goal_evaluation")

    catalog = SkillLoader([root]).load()

    found = catalog.find_by_capability("observation_triage")
    assert [s.name for s in found] == ["world_event_interpretation"]
    assert catalog.find_by_capability("nothing_provides_this") == []


def test_catalog_registers_as_definitions_capabilities_and_bindings(tmp_path):
    root = tmp_path / "skills"
    skill(root, "world_event_interpretation")
    runtime = Runtime(tmp_path / "skills.db")
    catalog = SkillLoader([root]).load()

    imported = runtime.import_skill_catalog(
        catalog, adapter=DirectorySkillAdapter(transport)
    )

    assert [item.status for item in imported] == ["ENABLED"]
    definition = runtime.get_definition("world_event_interpretation", "1")
    assert definition.metadata["role"] == "skill"
    assert definition.metadata["instructions"].startswith("# Interpret")
    assert runtime.get_process_capabilities("world_event_interpretation", "1")
    assert len(runtime.get_provider_bindings("world_event_interpretation", "1")) == 1
    runtime.close()


def test_a_skill_can_bind_to_an_already_registered_agent_provider(tmp_path):
    root = tmp_path / "skills"
    skill(root, "goal_evaluation")
    runtime = Runtime(tmp_path / "skills.db")
    provider = runtime.register_provider(
        ExecutionProvider(
            name="observer_agent",
            version="1",
            kind=ProviderKind.EXTERNAL_AGENT,
            adapter_name="a2a:observer_agent:1",
            health=ProviderHealth.HEALTHY,
        ),
        object(),
    )
    catalog = SkillLoader([root]).load()

    imported = runtime.import_skill_catalog(
        catalog, provider_for=lambda descriptor: provider.id
    )

    assert imported[0].provider_id == provider.id
    binding = runtime.get_provider_bindings("goal_evaluation", "1")[0]
    assert binding.provider_id == provider.id
    runtime.close()


def test_binding_stays_ineligible_when_the_provider_lacks_a_permission(tmp_path):
    root = tmp_path / "skills"
    skill(root, "goal_evaluation", manifest={"required_permissions": ["network.http"]})
    runtime = Runtime(tmp_path / "skills.db")
    provider = runtime.register_provider(
        ExecutionProvider(
            name="observer_agent",
            version="1",
            kind=ProviderKind.EXTERNAL_AGENT,
            adapter_name="a2a:observer_agent:1",
            health=ProviderHealth.HEALTHY,
        ),
        object(),
    )
    catalog = SkillLoader([root]).load()

    imported = runtime.import_skill_catalog(
        catalog,
        provider_for=lambda descriptor: provider.id,
        allowed_permissions=("network.http",),
    )

    assert imported[0].status == "UNAVAILABLE"
    assert "does not declare" in imported[0].reasons[-1]
    definition = runtime.get_definition("goal_evaluation", "1")
    assert not runtime.providers.has_eligible_provider(definition)
    runtime.close()


def test_shipped_skill_packages_are_valid():
    catalog = SkillLoader([REPO_SKILLS], strict=True).load()

    names = sorted(s.name for s in catalog.list())
    assert names == [
        "goal_evaluation",
        "project_planning",
        "work_assignment",
        "work_generation",
        "world_event_interpretation",
    ]
    interpretation = catalog.get("world_event_interpretation")
    assert interpretation.descriptor.output_schema["type"] == "object"
