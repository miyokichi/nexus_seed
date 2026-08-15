"""Directory skill inspection and safe conversion into provider records."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..capabilities.models import Capability, CapabilityRef
from ..core.process import ProcessContext, ProcessDefinition
from .adapters import ProviderUnavailableBeforeStart
from .models import (
    DelegationRequest,
    DelegationResult,
    ExecutionProvider,
    ImportedSkill,
    ProviderBinding,
    ProviderHealth,
    ProviderKind,
    ProviderStatus,
    SkillDescriptor,
)


class SkillValidationError(ValueError):
    """A skill package did not satisfy its explicit contract or safety rules."""


class DirectorySkillAdapter:
    """Translate one directory package without exposing its format to Core.

    ``skill.json`` is the machine-readable contract and ``SKILL.md`` is the
    instruction body.  Natural-language instructions are deliberately never
    mined for capabilities or permissions.
    """

    def __init__(
        self,
        execute: Callable[[DelegationRequest], Awaitable[DelegationResult]] | None = None,
    ) -> None:
        self._execute = execute

    @property
    def ready(self) -> bool:
        """Whether this adapter has an application-supplied execution transport."""
        return self._execute is not None

    def inspect_skill(self, source: str | Path) -> SkillDescriptor:
        """Read a directory package into the source-neutral SkillDescriptor."""
        root = Path(source).resolve()
        if not root.is_dir():
            raise SkillValidationError(f"skill source is not a directory: {root}")
        manifest_path = root / "skill.json"
        instructions_path = root / "SKILL.md"
        if not manifest_path.is_file() or not instructions_path.is_file():
            raise SkillValidationError("directory skill requires skill.json and SKILL.md")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillValidationError(f"invalid skill.json: {exc}") from exc
        try:
            instructions = instructions_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillValidationError(f"cannot read SKILL.md: {exc}") from exc

        resources = []
        resource_root = root / "resources"
        if resource_root.is_dir():
            resources = [
                str(path.relative_to(root)).replace("\\", "/")
                for path in sorted(resource_root.rglob("*"))
                if path.is_file()
            ]
        metadata = dict(manifest.get("metadata") or {})
        metadata["source_root"] = str(root)
        metadata["contains_scripts"] = bool(
            (root / "scripts").is_dir()
            and any(path.is_file() for path in (root / "scripts").rglob("*"))
        )
        descriptor = SkillDescriptor(
            name=str(manifest.get("name") or ""),
            version=str(manifest.get("version") or ""),
            description=str(manifest.get("description") or ""),
            provided_capabilities=list(manifest.get("provided_capabilities") or []),
            input_ports=list(manifest.get("input_ports") or []),
            output_ports=list(manifest.get("output_ports") or []),
            required_permissions=list(manifest.get("required_permissions") or []),
            instructions=instructions,
            resources=resources,
            execution_kind=str(manifest.get("execution_kind") or "EXTERNAL_SKILL"),
            metadata=metadata,
        )
        self.validate_skill(descriptor)
        return descriptor

    def validate_skill(self, descriptor: SkillDescriptor) -> None:
        """Validate explicit schema; never infer claims from SKILL.md."""
        if not descriptor.name or not descriptor.version:
            raise SkillValidationError("skill name and version are required")
        if descriptor.execution_kind != "EXTERNAL_SKILL":
            raise SkillValidationError("execution_kind must be EXTERNAL_SKILL")
        if not descriptor.provided_capabilities:
            raise SkillValidationError("provided_capabilities must be explicit and non-empty")
        for item in descriptor.provided_capabilities:
            if not isinstance(item, dict) or not item.get("name"):
                raise SkillValidationError("each provided capability needs a name")
        for field_name, ports in (
            ("input_ports", descriptor.input_ports),
            ("output_ports", descriptor.output_ports),
        ):
            if not all(isinstance(port, str) and port.strip() for port in ports):
                raise SkillValidationError(f"{field_name} must contain non-empty strings")
        if len(set(descriptor.required_permissions)) != len(
            descriptor.required_permissions
        ):
            raise SkillValidationError("required_permissions contains duplicates")

    def prepare_provider(self, descriptor: SkillDescriptor) -> ExecutionProvider:
        """Prepare the provider record after inspection and validation."""
        self.validate_skill(descriptor)
        active = self.ready
        return ExecutionProvider(
            name=f"skill:{descriptor.name}",
            version=descriptor.version,
            kind=ProviderKind.EXTERNAL_SKILL,
            adapter_name=f"directory_skill:{descriptor.name}:{descriptor.version}",
            status=ProviderStatus.ACTIVE if active else ProviderStatus.UNAVAILABLE,
            health=ProviderHealth.UNKNOWN if active else ProviderHealth.UNAVAILABLE,
            declared_permissions=tuple(descriptor.required_permissions),
            metadata={
                "skill_source": descriptor.metadata.get("source_root"),
                "descriptor": descriptor.to_dict(),
            },
        )

    async def execute(self, request: DelegationRequest) -> DelegationResult:
        """Delegate through the application transport; never execute scripts here."""
        if self._execute is None:
            raise ProviderUnavailableBeforeStart(
                "directory skill has no configured execution transport"
            )
        return await self._execute(request)


async def external_provider_proxy(ctx: ProcessContext):
    """Handler identity for external-only definitions; ProviderRegistry intercepts it."""
    return ctx.fail("external provider routing was not configured")


class SkillImporter:
    """Apply the inspect/validate/permission/install/register/enable pipeline."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime

    def import_directory(
        self,
        source: str | Path,
        adapter: DirectorySkillAdapter,
        *,
        allowed_permissions: tuple[str, ...] = (),
    ) -> ImportedSkill:
        """Import one directory skill without bypassing Phase 5C authority."""
        descriptor = adapter.inspect_skill(source)
        requested = set(descriptor.required_permissions)
        allowed = set(allowed_permissions)
        if not requested.issubset(allowed):
            denied = sorted(requested - allowed)
            raise SkillValidationError(f"skill requests ungranted permissions: {denied}")
        if descriptor.metadata.get("contains_scripts"):
            activation = self.runtime.installation_store.activation_for_definition(
                descriptor.name, descriptor.version
            )
            if activation is None:
                raise SkillValidationError(
                    "skill contains executable code without an active Phase 5C installation"
                )

        for claim in descriptor.provided_capabilities:
            self.runtime.register_capability(
                Capability(
                    name=str(claim["name"]),
                    version=str(claim.get("version") or "1"),
                    description=claim.get("description") or descriptor.description,
                    input_types=list(claim.get("input_types") or descriptor.input_ports),
                    output_types=list(claim.get("output_types") or descriptor.output_ports),
                    tags=list(claim.get("tags") or ["imported_skill"]),
                    metadata={"skill_source": str(Path(source).resolve())},
                )
            )
        refs = tuple(
            CapabilityRef(str(claim["name"]), str(claim.get("version") or "1"))
            for claim in descriptor.provided_capabilities
        )
        definition = ProcessDefinition(
            name=descriptor.name,
            version=descriptor.version,
            handler=f"external_provider_proxy:{descriptor.name}:{descriptor.version}",
            metadata={
                "role": "skill",
                "description": descriptor.description,
                "permissions": list(descriptor.required_permissions),
                "input_types": list(descriptor.input_ports),
                "output_types": list(descriptor.output_ports),
                "resources": list(descriptor.resources),
                "external_provider_only": True,
                "skill_source": str(Path(source).resolve()),
            },
            provides_capabilities=refs,
        )
        self.runtime.register_process(
            definition, external_provider_proxy, bind_internal=False
        )
        provider = self.runtime.register_provider(
            adapter.prepare_provider(descriptor), adapter
        )
        binding = self.runtime.register_provider_binding(
            ProviderBinding(
                process_definition_name=definition.name,
                process_definition_version=definition.version,
                provider_id=provider.id,
                priority=100,
                required_permissions=tuple(descriptor.required_permissions),
                metadata={"skill_source": str(Path(source).resolve())},
            )
        )
        imported = ImportedSkill(
            source=str(Path(source).resolve()),
            descriptor=descriptor,
            status="ENABLED" if provider.operational else "UNAVAILABLE",
            provider_id=provider.id,
            process_definition_name=definition.name,
            process_definition_version=definition.version,
            reasons=[f"provider binding {binding.id} registered"],
        )
        return self.runtime.provider_store.save_imported_skill(imported)
