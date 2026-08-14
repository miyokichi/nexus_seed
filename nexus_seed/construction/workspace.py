"""Dedicated workspaces and the sandbox-scoped Action backend."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from ..backends.action import (
    ActionCapability,
    ActionRequest,
    ActionResult,
    BackendCapabilities,
)
from ..resources.scope import ResourceScope, ScopeViolation
from .models import ConstructionGrantStatus, SandboxWorkspaceStatus


BACKEND_NAME = "construction_sandbox"
JOURNAL_NAME = ".nexus_seed_construction_journal.json"


class SandboxWorkspaceManager:
    """Create and resolve durable per-plan roots below a non-production base."""

    def __init__(self, base_root: str | Path | None = None) -> None:
        base = Path(base_root) if base_root else Path(tempfile.gettempdir()) / "nexus-seed-construction"
        self.base_root = base.resolve()
        self.base_root.mkdir(parents=True, exist_ok=True)
        self.scope = ResourceScope.for_root(self.base_root)

    def root_for(self, workspace_id) -> Path:
        return self.scope.resolve(str(workspace_id), write=True)

    def ensure(self, workspace) -> Path:
        """Idempotently materialize the workspace and reject locator drift."""
        expected = self.root_for(workspace.id)
        locator = Path(workspace.root_locator).resolve()
        if locator != expected:
            raise ScopeViolation("workspace locator does not match its durable identity")
        expected.mkdir(parents=True, exist_ok=True)
        return expected

    def make_locator(self, workspace_id) -> str:
        return str(self.root_for(workspace_id))

    def seal(self, workspace) -> None:
        workspace.status = SandboxWorkspaceStatus.SEALED


class ConstructionActionBackend:
    """Write artifacts only under the workspace named by an active Grant.

    The backend re-checks plan/workspace/grant identity at execution time.  An
    approved ActionProposal therefore cannot change its target to another
    workspace, and a human review cannot approve a production path: it is a
    permanent backend refusal rather than a reviewable risk decision.
    """

    def __init__(self, manager: SandboxWorkspaceManager, construction_store) -> None:
        self.manager = manager
        self.store = construction_store
        self.calls: list[ActionRequest] = []
        self._capabilities = BackendCapabilities(
            backend=BACKEND_NAME,
            actions={
                "write_artifact": ActionCapability(
                    "write_artifact", ("sandbox.write",), ("sandbox_write",)
                )
            },
        )

    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def validate_proposal(self, proposal) -> list[str]:
        """Pure scope preflight used before risk policy or human review."""
        from .validator import validate_relative_path

        params = proposal.parameters if isinstance(proposal.parameters, dict) else {}
        plan_id = params.get("construction_plan_id")
        workspace_id = params.get("workspace_id")
        relative = str(params.get("relative_path", ""))
        reason = validate_relative_path(relative)
        reasons = [reason] if reason else []
        if relative.replace("\\", "/") == JOURNAL_NAME:
            reasons.append("the construction journal is not an artifact target")
        try:
            import uuid
            workspace = self.store.get_workspace(uuid.UUID(str(workspace_id)))
            grant = self.store.get_grant_for_plan(uuid.UUID(str(plan_id)))
        except (ValueError, TypeError, AttributeError):
            workspace = grant = None
        if workspace is None or grant is None:
            reasons.append("construction workspace or grant does not exist")
            return reasons
        if workspace.construction_plan_id != grant.construction_plan_id or grant.workspace_id != workspace.id:
            reasons.append("construction workspace/grant identity mismatch")
        if grant.status is not ConstructionGrantStatus.ACTIVE:
            reasons.append("construction grant is not active")
        expected = f"{workspace.id}/{relative}".replace("\\", "/")
        if str(proposal.target or "").replace("\\", "/") != expected:
            reasons.append("action target is outside its construction workspace")
        if Path(grant.allowed_root).resolve() != Path(workspace.root_locator).resolve():
            reasons.append("construction grant root does not match the workspace")
        content = params.get("content")
        if not isinstance(content, str):
            reasons.append("construction artifact content must be text")
        elif len(content.encode("utf-8")) > workspace.max_file_bytes:
            reasons.append("construction artifact exceeds the per-file size limit")
        return list(dict.fromkeys(reasons))

    async def execute(self, request: ActionRequest) -> ActionResult:
        self.calls.append(request)
        started = time.monotonic()
        if request.action_type != "write_artifact":
            return ActionResult(success=False, error="unsupported construction action", retryable=False)
        plan_id = request.parameters.get("construction_plan_id")
        workspace_id = request.parameters.get("workspace_id")
        relative_path = request.parameters.get("relative_path")
        content = request.parameters.get("content")
        if not all((plan_id, workspace_id, relative_path)) or not isinstance(content, str):
            return ActionResult(success=False, error="incomplete construction action", retryable=False)
        try:
            import uuid
            workspace = self.store.get_workspace(uuid.UUID(str(workspace_id)))
            grant = self.store.get_grant_for_plan(uuid.UUID(str(plan_id)))
        except (ValueError, TypeError):
            workspace = grant = None
        if workspace is None or grant is None:
            return ActionResult(success=False, error="workspace or construction grant not found", retryable=False)
        if str(workspace.construction_plan_id) != str(plan_id) or grant.workspace_id != workspace.id:
            return ActionResult(success=False, error="construction identity mismatch", retryable=False)
        if grant.status is not ConstructionGrantStatus.ACTIVE:
            return ActionResult(success=False, error="construction grant is not active", retryable=False)
        if "sandbox.write" not in grant.allowed_permissions:
            return ActionResult(success=False, error="construction grant denies sandbox.write", retryable=False)
        if str(request.target or "") != f"{workspace_id}/{relative_path}".replace("\\", "/"):
            return ActionResult(success=False, error="action target is outside its workspace", retryable=False)
        try:
            root = self.manager.ensure(workspace)
            scope = ResourceScope.for_root(root, create=False)
            target = scope.resolve(str(relative_path), write=True)
            # Existing symlinks are resolved by ResourceScope; refuse links in
            # any parent explicitly so a link cannot be swapped later.
            cursor = root
            for part in target.relative_to(root).parts:
                cursor = cursor / part
                if cursor.exists() and cursor.is_symlink():
                    raise ScopeViolation("symlink targets are forbidden in construction workspaces")
        except (ScopeViolation, OSError, ValueError) as exc:
            return ActionResult(success=False, error=str(exc), retryable=False)

        data = content.encode("utf-8")
        if len(data) > workspace.max_file_bytes:
            return ActionResult(success=False, error="artifact exceeds per-file size limit", retryable=False)
        existing_files = [p for p in root.rglob("*") if p.is_file() and p.name != JOURNAL_NAME]
        new_file = not target.exists()
        if new_file and len(existing_files) >= workspace.max_files:
            return ActionResult(success=False, error="workspace file count limit exceeded", retryable=False)
        existing_total = sum(p.stat().st_size for p in existing_files if p != target)
        if existing_total + len(data) > workspace.max_total_bytes:
            return ActionResult(success=False, error="workspace total size limit exceeded", retryable=False)

        journal_path = root / JOURNAL_NAME
        if journal_path.is_symlink() or journal_path.with_suffix(".tmp").is_symlink():
            return ActionResult(
                success=False, error="construction journal symlink is forbidden",
                retryable=False,
            )
        journal = self._read_journal(journal_path)
        key = request.idempotency_key
        if key and key in journal:
            return ActionResult(success=True, output=dict(journal[key]), duplicate=True)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
            output = {"path": str(target), "relative_path": str(relative_path), "bytes_written": len(data)}
            if key:
                journal[key] = output
                self._write_journal(journal_path, journal)
        except OSError as exc:
            return ActionResult(success=False, error=str(exc), retryable=True)
        return ActionResult(
            success=True, output=output,
            latency_ms=(time.monotonic() - started) * 1000.0,
        )

    @staticmethod
    def _read_journal(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write_journal(path: Path, value: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        shutil.move(str(tmp), str(path))


__all__ = ["BACKEND_NAME", "ConstructionActionBackend", "SandboxWorkspaceManager"]
