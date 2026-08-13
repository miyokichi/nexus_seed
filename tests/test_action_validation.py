"""Schema + backend-capability validation (spec §10-§12)."""

from __future__ import annotations

from nexus_seed.actions.models import ActionProposal, RiskLevel
from nexus_seed.actions.validation import validate_action_proposal, validate_schema
from nexus_seed.backends.action import (
    FAKE_CAPABILITIES,
    ActionCapability,
    BackendCapabilities,
    FakeActionBackend,
    capabilities_of,
)

GRANTED = ["filesystem.write", "filesystem.read"]


def valid_proposal(**kw) -> ActionProposal:
    defaults = dict(
        backend="fake_action",
        action_type="write_file",
        target="out.txt",
        parameters={"content": "x"},
        required_permissions=["filesystem.write"],
        declared_side_effects=["filesystem_write"],
        risk_level=RiskLevel.LOW,
    )
    defaults.update(kw)
    return ActionProposal(**defaults)


def test_valid_proposal_passes_every_stage():
    result = validate_action_proposal(
        valid_proposal(), capabilities=FAKE_CAPABILITIES, granted_permissions=GRANTED
    )
    assert result.ok
    assert result.reasons == []
    assert result.mandatory_permissions == ["filesystem.write"]


def test_empty_backend_and_action_type_fail_schema():
    result = validate_schema(valid_proposal(backend="", action_type=""))
    assert not result.schema_ok
    assert "empty backend" in result.reasons
    assert "empty action_type" in result.reasons


def test_wrongly_typed_fields_fail_schema():
    proposal = valid_proposal()
    proposal.parameters = "content=x"
    proposal.required_permissions = "filesystem.write"
    proposal.declared_side_effects = None
    proposal.risk_level = "VERY BAD"
    result = validate_schema(proposal)
    assert not result.schema_ok
    assert len(result.reasons) == 4


def test_schema_failure_short_circuits_later_stages():
    """Nothing downstream may read fields the schema stage rejected."""
    result = validate_action_proposal(
        valid_proposal(backend=""), capabilities=None, granted_permissions=GRANTED
    )
    assert not result.ok
    assert not result.capability_ok and not result.permission_ok


def test_unregistered_backend_is_refused():
    result = validate_action_proposal(
        valid_proposal(), capabilities=None, granted_permissions=GRANTED
    )
    assert not result.capability_ok
    assert "not registered" in result.reasons[0]


def test_backend_that_cannot_do_the_action_is_refused():
    limited = BackendCapabilities(
        backend="fake_action",
        actions={"read_file": ActionCapability("read_file", ("filesystem.read",))},
    )
    result = validate_action_proposal(
        valid_proposal(), capabilities=limited, granted_permissions=GRANTED
    )
    assert not result.capability_ok
    assert "cannot perform 'write_file'" in result.reasons[0]


def test_capability_registry_is_a_deterministic_lookup():
    backend = FakeActionBackend()
    capabilities = capabilities_of(backend)
    assert capabilities.supports("write_file")
    assert not capabilities.supports("launch_missile")
    assert capabilities.get("launch_missile") is None


def test_backend_without_capabilities_is_unusable_for_actions():
    class Bare:
        async def execute(self, request):  # pragma: no cover - never called
            raise AssertionError("must not be called")

    assert capabilities_of(Bare()) is None
