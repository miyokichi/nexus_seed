"""Directory Skill import is explicit and respects permissions/installation."""

from __future__ import annotations

import json

import pytest

from nexus_seed.providers import (
    DelegationResult,
    DelegationStatus,
    DirectorySkillAdapter,
    SkillValidationError,
)
from nexus_seed.runtime.runtime import Runtime


def package(root, *, permissions=None, scripts=False):
    root.mkdir()
    (root / "SKILL.md").write_text("# Summarize\nFollow the supplied contract.", encoding="utf-8")
    (root / "skill.json").write_text(
        json.dumps(
            {
                "name": "summarize_document",
                "version": "1",
                "description": "Create a summary",
                "provided_capabilities": [
                    {
                        "name": "summarize_document",
                        "input_types": ["document"],
                        "output_types": ["summary"],
                    }
                ],
                "input_ports": ["document"],
                "output_ports": ["summary"],
                "required_permissions": permissions or [],
                "execution_kind": "EXTERNAL_SKILL",
            }
        ),
        encoding="utf-8",
    )
    if scripts:
        scripts_dir = root / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.py").write_text("print('unsafe')", encoding="utf-8")
    return root


async def transport(request):
    return DelegationResult(
        invocation_id=request.invocation_id,
        status=DelegationStatus.COMPLETED,
        typed_outputs=[{"type": "summary", "value": "short"}],
    )


def test_directory_skill_import_builds_standard_records(tmp_path):
    source = package(tmp_path / "skill")
    runtime = Runtime(tmp_path / "skill.db")

    imported = runtime.import_directory_skill(
        source, DirectorySkillAdapter(transport)
    )

    assert imported.status == "ENABLED"
    assert runtime.get_definition("summarize_document", "1") is not None
    assert runtime.get_process_capabilities("summarize_document", "1")[0].name == "summarize_document"
    assert len(runtime.get_provider_bindings("summarize_document", "1")) == 1
    assert runtime.get_imported_skills()[0].descriptor.instructions.startswith("# Summarize")
    runtime.close()


def test_natural_language_alone_is_not_a_capability_contract(tmp_path):
    source = tmp_path / "skill"
    source.mkdir()
    (source / "SKILL.md").write_text("I can safely do everything", encoding="utf-8")

    with pytest.raises(SkillValidationError, match="skill.json"):
        DirectorySkillAdapter(transport).inspect_skill(source)


def test_ungranted_permissions_are_rejected_before_registration(tmp_path):
    source = package(tmp_path / "skill", permissions=["network.http"])
    runtime = Runtime(tmp_path / "skill.db")

    with pytest.raises(SkillValidationError, match="ungranted permissions"):
        runtime.import_directory_skill(source, DirectorySkillAdapter(transport))

    assert runtime.get_definition("summarize_document", "1") is None
    runtime.close()


def test_raw_script_cannot_bypass_phase_5c(tmp_path):
    source = package(tmp_path / "skill", scripts=True)
    runtime = Runtime(tmp_path / "skill.db")

    with pytest.raises(SkillValidationError, match="Phase 5C"):
        runtime.import_directory_skill(source, DirectorySkillAdapter(transport))

    assert runtime.get_execution_providers()[-1].name == "local_runtime"
    runtime.close()
