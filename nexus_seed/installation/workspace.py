"""Versioned production root and its grant-aware Action backend."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import tempfile
import time
from pathlib import Path

from ..backends.action import ActionCapability, ActionRequest, ActionResult, BackendCapabilities
from ..resources.scope import ResourceScope, ScopeViolation
from .models import InstallationGrantStatus, InstallationPlanStatus
from .validator import sha256_file


BACKEND_NAME = "installation_production"
JOURNAL_NAME = ".nexus_seed_installation_journal.json"


class InstalledExtractor:
    """Adapter exposing a verified installed ``nexus_capability`` as Extractor."""

    def __init__(self, activation, manager) -> None:
        self.activation = activation
        self.manager = manager
        self.name = activation.component_name
        self.version = activation.component_version
        self.representation_type = "structure"

    def supports(self, resource_type: str) -> bool:
        return bool(resource_type)

    def extract(self, data: bytes):
        function = self.manager.capability_function(self.activation)
        return function({"data": data.decode("utf-8", errors="replace")})


class ProductionInstallationManager:
    """Resolve only versioned paths below a dedicated production data root."""

    def __init__(self, root: str | Path | None = None) -> None:
        base = Path(root) if root else Path(tempfile.gettempdir()) / "nexus-seed-installed"
        self.root = base.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.scope = ResourceScope.for_root(self.root)

    def resolve(self, relative: str, *, write: bool = True) -> Path:
        path = self.scope.resolve(relative, write=write)
        if path == self.root:
            raise ScopeViolation("production root itself is not an installation destination")
        return path

    def component_root(self, component: str, version: str) -> Path:
        return self.resolve(f"{component}/{version}")

    def implementation_path(self, activation) -> Path:
        root = Path(activation.installed_root).resolve()
        if root != self.component_root(activation.component_name, activation.component_version):
            raise ScopeViolation("active component root does not match versioned identity")
        candidates = sorted(
            p for p in root.rglob("*.py")
            if "tests" not in p.relative_to(root).parts and not p.name.startswith("test_")
        )
        if not candidates:
            raise FileNotFoundError("installed implementation module is missing")
        return candidates[0]

    def capability_function(self, activation):
        path = self.implementation_path(activation)
        module_name = "nexus_seed_installed_" + hashlib.sha256(
            f"{activation.id}:{path}".encode("utf-8")
        ).hexdigest()[:16]
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load installed module {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        function = getattr(module, "nexus_capability", None)
        if not callable(function):
            raise AttributeError("installed module does not expose nexus_capability")
        return function

    def smoke(self, activation) -> dict:
        function = self.capability_function(activation)
        output = function({"phase": "production_smoke", "side_effects": False})
        if output is None:
            raise ValueError("production smoke returned no output")
        return {"loadable": True, "output_type": type(output).__name__}

    def register_memory_component(self, activation, extractor_registry) -> None:
        if activation.strategy == "ADD_EXTRACTOR":
            current = extractor_registry.get(activation.component_name)
            if current is None or getattr(current, "version", None) != activation.component_version:
                extractor_registry.register(InstalledExtractor(activation, self))

    def restore_active(self, installation_store, extractor_registry) -> None:
        """Rebuild process-local component registrations from durable ACTIVE rows."""
        for activation in installation_store.active_activations():
            try:
                self.register_memory_component(activation, extractor_registry)
            except (OSError, ImportError, SyntaxError, AttributeError, ValueError):
                # The durable row remains authoritative.  Activation processes
                # and health checks report a broken component; startup never
                # invents a replacement or silently changes registry state.
                continue


class InstallationActionBackend:
    """Copy/rollback only exact artifacts named by an active InstallationGrant."""

    def __init__(self, manager: ProductionInstallationManager, store, resource_store) -> None:
        self.manager = manager
        self.store = store
        self.resource_store = resource_store
        self.calls: list[ActionRequest] = []
        self._capabilities = BackendCapabilities(
            backend=BACKEND_NAME,
            actions={
                "copy_verified_artifact": ActionCapability(
                    "copy_verified_artifact", ("production.install",), ("filesystem_write",)
                ),
                "rollback_installation": ActionCapability(
                    "rollback_installation", ("production.install",), ("filesystem_write",)
                ),
            },
        )

    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def validate_proposal(self, proposal) -> list[str]:
        params = proposal.parameters if isinstance(proposal.parameters, dict) else {}
        try:
            import uuid
            plan = self.store.get_plan(uuid.UUID(str(params.get("installation_plan_id"))))
            grant = self.store.grant_for_plan(plan.id) if plan else None
        except (ValueError, TypeError):
            plan = grant = None
        if plan is None or grant is None:
            return ["installation plan or InstallationGrant does not exist"]
        reasons: list[str] = []
        if grant.status is not InstallationGrantStatus.ACTIVE:
            reasons.append("InstallationGrant is not active")
        if str(params.get("installation_grant_id", "")) != str(grant.id):
            reasons.append("action does not name the plan's exact InstallationGrant")
        if proposal.action_type == "copy_verified_artifact":
            reasons.extend(self._validate_copy(proposal, plan, grant))
        elif proposal.action_type == "rollback_installation":
            expected = str((plan.rollback_spec or {}).get("new_installed_root", ""))
            if str(proposal.target or "") != expected:
                reasons.append("rollback target is outside the reviewed installation")
            if plan.status not in {InstallationPlanStatus.FAILED, InstallationPlanStatus.ROLLING_BACK}:
                reasons.append("rollback is allowed only for a failed installation")
        return list(dict.fromkeys(reasons))

    def _validate_copy(self, proposal, plan, grant) -> list[str]:
        params = proposal.parameters
        destination = str(params.get("destination", ""))
        content_hash = str(params.get("content_hash", ""))
        version_id = str(params.get("resource_version_id", ""))
        reasons: list[str] = []
        if plan.status not in {InstallationPlanStatus.APPROVED, InstallationPlanStatus.INSTALLING}:
            reasons.append(f"installation plan is {plan.status.value}, not approved for copy")
        if destination not in grant.allowed_destinations or destination not in plan.production_destinations:
            reasons.append("destination is outside the InstallationGrant")
        if str(proposal.target or "") != destination:
            reasons.append("action target does not equal its exact reviewed destination")
        if content_hash not in grant.allowed_artifact_hashes:
            reasons.append("artifact hash is outside the InstallationGrant")
        artifact = next(
            (a for a in plan.artifact_versions if str(a.resource_version_id) == version_id), None
        )
        if artifact is None:
            reasons.append("ResourceVersion is not one of the verified construction artifacts")
            return reasons
        if artifact.destination != destination or artifact.content_hash != content_hash:
            reasons.append("artifact identity does not match the InstallationPlan")
        version = self.resource_store.get_version(artifact.resource_version_id)
        if version is None or str(version.content_hash or "") != content_hash:
            reasons.append("ResourceVersion hash no longer matches the plan")
        try:
            self.manager.resolve(destination)
            if sha256_file(artifact.locator) != content_hash:
                reasons.append("artifact bytes changed after verification")
        except (OSError, ScopeViolation, ValueError) as exc:
            reasons.append(str(exc))
        return reasons

    async def execute(self, request: ActionRequest) -> ActionResult:
        self.calls.append(request)
        started = time.monotonic()
        class _Proposal:
            action_type = request.action_type
            target = request.target
            parameters = request.parameters
        reasons = self.validate_proposal(_Proposal())
        if reasons:
            return ActionResult(success=False, error="; ".join(reasons), retryable=False)
        journal = self._read_journal()
        if request.idempotency_key and request.idempotency_key in journal:
            return ActionResult(success=True, output=journal[request.idempotency_key], duplicate=True)
        try:
            if request.action_type == "copy_verified_artifact":
                source = Path(request.parameters["source_locator"]).resolve()
                destination = self.manager.resolve(request.parameters["destination"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(destination.suffix + ".installing")
                shutil.copyfile(source, temporary)
                if sha256_file(temporary) != request.parameters["content_hash"]:
                    temporary.unlink(missing_ok=True)
                    return ActionResult(success=False, error="copied artifact hash mismatch", retryable=False)
                temporary.replace(destination)
                output = {"destination": str(destination), "content_hash": request.parameters["content_hash"]}
            elif request.action_type == "rollback_installation":
                root = self.manager.resolve(str(request.target))
                if root.exists():
                    shutil.rmtree(root)
                output = {"removed": str(root)}
            else:
                return ActionResult(success=False, error="unsupported installation action", retryable=False)
            if request.idempotency_key:
                journal[request.idempotency_key] = output
                self._write_journal(journal)
            return ActionResult(
                success=True, output=output,
                latency_ms=(time.monotonic() - started) * 1000.0,
            )
        except OSError as exc:
            return ActionResult(success=False, error=str(exc), retryable=True)

    def _journal_path(self) -> Path:
        return self.manager.root / JOURNAL_NAME

    def _read_journal(self) -> dict:
        try:
            value = json.loads(self._journal_path().read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_journal(self, value: dict) -> None:
        path = self._journal_path()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        temporary.replace(path)


__all__ = [
    "BACKEND_NAME", "InstallationActionBackend", "InstalledExtractor",
    "ProductionInstallationManager",
]
