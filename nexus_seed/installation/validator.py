"""Pure validation before a production installation can be reviewed."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..construction.models import ConstructionResultStatus, SandboxWorkspaceStatus
from ..extension.strategies import CRITICAL_PERMISSIONS
from .models import InstallationStepType


FORBIDDEN_INSTALLATION_PERMISSIONS = frozenset({
    "runtime.modify", "permission.modify", "host.admin", "network.unrestricted",
    "shell.execute", "plugin.install", "arbitrary.package.install",
})


@dataclass(frozen=True)
class InstallationValidation:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(block)
    # ResourceVersion uses the explicit algorithm-prefixed identity so hashes
    # remain comparable even if another algorithm is introduced later.
    return f"sha256-{digest.hexdigest()}"


def confined_relative(value: str) -> bool:
    if not value or "\x00" in value:
        return False
    posix = PurePosixPath(value.replace("\\", "/"))
    win = PureWindowsPath(value)
    return not (posix.is_absolute() or win.is_absolute() or win.drive or ".." in posix.parts)


class InstallationValidator:
    """Refuse mutation, scope drift, core changes and non-rollbackable plans."""

    def validate(
        self,
        plan,
        *,
        construction_result,
        construction_plan,
        proposal,
        workspace,
        resource_store,
        production_manager,
    ) -> InstallationValidation:
        reasons: list[str] = []
        if construction_result is None or construction_result.status is not ConstructionResultStatus.VERIFIED:
            reasons.append("only a VERIFIED ConstructionResult may be installed")
        elif plan.construction_result_id != construction_result.id:
            reasons.append("plan references a different ConstructionResult")
        if construction_plan is None or proposal is None:
            reasons.append("construction plan or extension proposal is missing")
        if workspace is None or workspace.status is not SandboxWorkspaceStatus.SEALED:
            reasons.append("construction workspace must be SEALED")
        if proposal is not None:
            if not set(plan.target_capabilities) <= set(proposal.target_names):
                reasons.append("target capability is outside the approved ExtensionProposal")
            if not set(plan.required_permissions) <= set(proposal.required_permissions):
                reasons.append("installation permissions exceed the ExtensionProposal ceiling")
        forbidden = set(plan.required_permissions) & (
            FORBIDDEN_INSTALLATION_PERMISSIONS | set(CRITICAL_PERMISSIONS)
        )
        if forbidden:
            reasons.append(f"forbidden installation permission(s): {sorted(forbidden)}")
        if not plan.artifact_versions:
            reasons.append("verified artifact versions are required")
        if set(plan.production_destinations) != {a.destination for a in plan.artifact_versions}:
            reasons.append("production destination set does not match artifact bindings")
        known_steps = set(InstallationStepType)
        for step in plan.steps:
            if step.step_type not in known_steps:
                reasons.append(f"unknown installation step {step.step_type!r}")
        for artifact in plan.artifact_versions:
            version = resource_store.get_version(artifact.resource_version_id)
            if version is None or version.resource_id != artifact.resource_id:
                reasons.append(f"artifact ResourceVersion {artifact.resource_version_id} does not exist")
                continue
            if str(version.content_hash or "") != artifact.content_hash:
                reasons.append(f"ResourceVersion hash mismatch for {artifact.relative_path}")
            if not confined_relative(artifact.destination):
                reasons.append(f"arbitrary production destination rejected: {artifact.destination!r}")
            else:
                try:
                    production_manager.resolve(artifact.destination)
                except (ValueError, OSError) as exc:
                    reasons.append(str(exc))
            try:
                current = sha256_file(artifact.locator)
            except OSError as exc:
                reasons.append(f"artifact unavailable for {artifact.relative_path}: {exc}")
            else:
                if current != artifact.content_hash:
                    reasons.append(f"artifact hash changed after verification: {artifact.relative_path}")
        rollback = plan.rollback_spec or {}
        if rollback.get("strategy") != "disable_new_and_restore_previous":
            reasons.append("installation strategy has no supported rollback")
        for change in plan.registry_changes:
            if change.get("modifies_core") or change.get("modifies_policy"):
                reasons.append("runtime core or permission policy mutation is forbidden")
        return InstallationValidation(not reasons, list(dict.fromkeys(reasons)))


__all__ = [
    "FORBIDDEN_INSTALLATION_PERMISSIONS", "InstallationValidation",
    "InstallationValidator", "confined_relative", "sha256_file",
]
