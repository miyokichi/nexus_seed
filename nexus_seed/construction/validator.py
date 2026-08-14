"""Validation boundary between an approved proposal and executable construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..extension.models import ExtensionProposalStatus
from .models import ArtifactRole, ConstructionStepType, NetworkPolicy


FORBIDDEN_PERMISSIONS = frozenset({
    "production.write", "runtime.modify", "permission.modify", "plugin.install",
    "host.admin", "network.unrestricted", "shell.execute",
})


@dataclass(frozen=True)
class ConstructionValidation:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def sandbox_permissions_for(proposal_permissions: list[str]) -> list[str]:
    """Translate proposal authority into construction-scoped powers only."""
    source = set(proposal_permissions)
    allowed = {"sandbox.read", "sandbox.test"}
    write_sources = {
        "repository.modify", "repository.read", "filesystem.write",
        "process.register", "process.configure", "capability.enable",
    }
    if source & write_sources:
        allowed.add("sandbox.write")
    return sorted(allowed)


def validate_relative_path(value: str) -> str | None:
    """Return a refusal reason, or ``None`` for a confined relative path."""
    if not value or "\x00" in value:
        return "path is empty or contains NUL"
    posix = PurePosixPath(value.replace("\\", "/"))
    win = PureWindowsPath(value)
    if posix.is_absolute() or win.is_absolute() or win.drive:
        return f"absolute path is forbidden: {value!r}"
    if any(part in {"..", ""} for part in posix.parts):
        return f"path traversal is forbidden: {value!r}"
    if value.startswith(("/", "\\")):
        return f"absolute path is forbidden: {value!r}"
    return None


class ConstructionValidator:
    """Validate a plan without changing either the workspace or production."""

    def validate(self, plan, proposal) -> ConstructionValidation:
        reasons: list[str] = []
        if proposal.status is not ExtensionProposalStatus.APPROVED:
            reasons.append("only an APPROVED ExtensionProposal may be constructed")
        if plan.extension_proposal_id != proposal.id:
            reasons.append("plan references a different extension proposal")
        if not set(plan.target_capabilities) <= set(proposal.target_names):
            reasons.append("target capability is outside the approved proposal")
        if not plan.target_capabilities:
            reasons.append("at least one target capability is required")
        if not set(plan.required_permissions) <= set(proposal.required_permissions):
            reasons.append("plan permissions exceed the approved proposal ceiling")
        forbidden = set(plan.required_permissions) & FORBIDDEN_PERMISSIONS
        if forbidden:
            reasons.append(f"forbidden construction permission(s): {sorted(forbidden)}")
        if not plan.expected_artifacts:
            reasons.append("expected artifacts are required")
        if not plan.verification_requirements:
            reasons.append("verification requirements are required")
        layers = {str(v.get("layer", "")).upper() for v in plan.verification_requirements}
        for required in ("STRUCTURAL", "STATIC", "BEHAVIOR"):
            if required not in layers:
                reasons.append(f"required verification layer missing: {required}")
        network = str(plan.sandbox_requirements.get("network", "DENY")).upper()
        if network != NetworkPolicy.DENY.value:
            reasons.append("construction network policy must be DENY")

        seen_steps: set[int] = set()
        allowed_sandbox = set(sandbox_permissions_for(proposal.required_permissions))
        for step in plan.steps:
            if step.step_index in seen_steps:
                reasons.append(f"duplicate construction step index {step.step_index}")
            seen_steps.add(step.step_index)
            if not isinstance(step.step_type, ConstructionStepType):
                reasons.append(f"unknown construction step type {step.step_type!r}")
            if not set(step.required_permissions) <= allowed_sandbox:
                reasons.append(
                    f"step {step.step_index} exceeds sandbox grant: "
                    f"{sorted(set(step.required_permissions) - allowed_sandbox)}"
                )
            for output in step.expected_outputs:
                reason = validate_relative_path(str(output.get("relative_path", "")))
                if reason:
                    reasons.append(reason)

        known_roles = {r.value for r in ArtifactRole}
        for artifact in plan.expected_artifacts:
            reason = validate_relative_path(artifact.relative_path)
            if reason:
                reasons.append(reason)
            if artifact.artifact_role not in known_roles:
                reasons.append(f"unknown artifact role {artifact.artifact_role!r}")
        return ConstructionValidation(ok=not reasons, reasons=list(dict.fromkeys(reasons)))


__all__ = [
    "ConstructionValidation", "ConstructionValidator", "FORBIDDEN_PERMISSIONS",
    "sandbox_permissions_for", "validate_relative_path",
]
