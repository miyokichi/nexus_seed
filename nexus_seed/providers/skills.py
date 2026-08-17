"""Directory skill inspection, loading and safe conversion into provider records.

A Skill here is a *reusable cognitive procedure*: ``skill.json`` states the
machine-readable contract (capabilities, typed ports, permissions) and
``SKILL.md`` states how to think about the problem.  A Skill is never an agent
runtime and never a Core primitive — it becomes an ordinary ProcessDefinition
whose ``ProviderBinding`` decides *where* the thinking is executed
(Invariants 2, 125, 127).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
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

#: Instruction file used when ``skill.json`` does not name one.
DEFAULT_INSTRUCTION_FILE = "SKILL.md"

#: Keys ``skill.json`` may declare.  Anything else is a contract error rather
#: than a silently ignored field, so typos cannot weaken a declared contract.
_MANIFEST_KEYS = frozenset(
    {
        "name", "version", "description", "provided_capabilities", "capabilities",
        "input_ports", "output_ports", "required_permissions", "execution_kind",
        "instruction", "enabled", "input_schema", "output_schema", "metadata",
    }
)


class SkillValidationError(ValueError):
    """A skill package did not satisfy its explicit contract or safety rules."""


def _capability_claims(manifest: dict) -> list[dict]:
    """Normalise both capability spellings into explicit claim dicts.

    ``provided_capabilities`` is the full form.  ``capabilities`` is the short
    form for the common case where a Skill provides one capability named after
    itself; it carries exactly the same weight and no more.
    """
    claims = list(manifest.get("provided_capabilities") or [])
    for entry in manifest.get("capabilities") or []:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            raise SkillValidationError("capabilities entries must be names or objects")
        if not any(
            isinstance(claim, dict) and claim.get("name") == entry.get("name")
            for claim in claims
        ):
            claims.append(entry)
    return claims


def _manifest_bool(manifest: dict, key: str, *, default: bool) -> bool:
    value = manifest.get(key, default)
    if not isinstance(value, bool):
        raise SkillValidationError(f"{key} must be true or false")
    return value


def _manifest_schema(manifest: dict, key: str) -> dict:
    value = manifest.get(key) or {}
    if not isinstance(value, dict):
        raise SkillValidationError(f"{key} must be a JSON object")
    return value


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
        if not manifest_path.is_file():
            raise SkillValidationError("directory skill requires skill.json and SKILL.md")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillValidationError(f"invalid skill.json: {exc}") from exc
        if not isinstance(manifest, dict):
            raise SkillValidationError("skill.json must contain a JSON object")
        unknown = sorted(set(manifest) - _MANIFEST_KEYS)
        if unknown:
            raise SkillValidationError(f"skill.json has unknown fields: {unknown}")

        instruction_name = str(manifest.get("instruction") or DEFAULT_INSTRUCTION_FILE)
        instructions_path = root / instruction_name
        if ".." in Path(instruction_name).parts or Path(instruction_name).is_absolute():
            raise SkillValidationError(
                f"instruction must stay inside the skill package: {instruction_name!r}"
            )
        if not instructions_path.is_file():
            raise SkillValidationError(
                f"directory skill requires skill.json and {instruction_name}"
            )
        try:
            instructions = instructions_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillValidationError(f"cannot read {instruction_name}: {exc}") from exc

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
        metadata["instruction_file"] = instruction_name
        metadata["contains_scripts"] = bool(
            (root / "scripts").is_dir()
            and any(path.is_file() for path in (root / "scripts").rglob("*"))
        )
        descriptor = SkillDescriptor(
            name=str(manifest.get("name") or ""),
            version=str(manifest.get("version") or ""),
            description=str(manifest.get("description") or ""),
            provided_capabilities=_capability_claims(manifest),
            input_ports=list(manifest.get("input_ports") or []),
            output_ports=list(manifest.get("output_ports") or []),
            required_permissions=list(manifest.get("required_permissions") or []),
            instructions=instructions,
            resources=resources,
            execution_kind=str(manifest.get("execution_kind") or "EXTERNAL_SKILL"),
            enabled=_manifest_bool(manifest, "enabled", default=True),
            input_schema=_manifest_schema(manifest, "input_schema"),
            output_schema=_manifest_schema(manifest, "output_schema"),
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
        if descriptor.output_schema and not descriptor.output_ports:
            raise SkillValidationError(
                "output_schema requires at least one declared output port"
            )

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


@dataclass(frozen=True)
class LoadedSkill:
    """One validated Skill package and where it was found."""

    descriptor: SkillDescriptor
    path: Path
    root: Path
    #: Paths in lower-precedence roots this Skill shadows.
    shadows: tuple[Path, ...] = ()

    @property
    def name(self) -> str:
        return self.descriptor.name

    @property
    def capability_names(self) -> tuple[str, ...]:
        return tuple(
            str(claim["name"]) for claim in self.descriptor.provided_capabilities
        )


@dataclass(frozen=True)
class SkillLoadFailure:
    """One package that could not be loaded, kept as data rather than a crash."""

    path: Path
    error: str
    name: str | None = None

    def __str__(self) -> str:
        return f"{self.path} ({self.name or 'unnamed'}): {self.error}"


@dataclass
class SkillCatalog:
    """Lookup over the Skills one load pass produced.

    Deliberately *not* a second durable registry (Invariant 124): capabilities
    still live in the CapabilityRegistry and execution still resolves through
    ProviderBinding.  This is the in-memory result of scanning directories.
    """

    skills: list[LoadedSkill] = field(default_factory=list)
    failures: list[SkillLoadFailure] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)

    def get(self, name: str) -> LoadedSkill | None:
        """Return the winning Skill for ``name``, or ``None``."""
        return next((skill for skill in self.skills if skill.name == name), None)

    def list(self) -> list[LoadedSkill]:
        """Return every loaded Skill in deterministic precedence order."""
        return list(self.skills)

    def find_by_capability(self, capability: str) -> list[LoadedSkill]:
        """Return Skills claiming ``capability``, highest precedence first."""
        return [s for s in self.skills if capability in s.capability_names]


class SkillLoader:
    """Scan configured roots and validate the Skill packages found there.

    The loader only produces data.  It never invokes an LLM, selects a
    provider, generates Work or touches World State — registration is the
    caller's explicit step through :class:`SkillImporter`.

    Precedence is positional and deterministic: the first root wins, so the
    conventional order is project-local, then user/global, then built-in.  A
    duplicate *inside one root* is always an error because no rule could
    resolve it non-arbitrarily.
    """

    def __init__(
        self,
        roots: Sequence[str | Path],
        *,
        adapter: DirectorySkillAdapter | None = None,
        strict: bool = False,
        on_duplicate: str = "override",
    ) -> None:
        if on_duplicate not in {"override", "error"}:
            raise ValueError("on_duplicate must be 'override' or 'error'")
        self.roots = [Path(root).expanduser() for root in roots]
        self.adapter = adapter or DirectorySkillAdapter()
        self.strict = strict
        self.on_duplicate = on_duplicate

    def load(self) -> SkillCatalog:
        """Load every enabled Skill, newest precedence first.

        Raises :class:`SkillValidationError` when ``strict`` is set; otherwise
        one broken package is recorded as a failure and the rest still load.
        """
        catalog = SkillCatalog()
        winners: dict[str, LoadedSkill] = {}
        shadowed: dict[str, list[Path]] = {}
        for root in self.roots:
            seen_in_root: dict[str, Path] = {}
            for path in self._packages(root):
                try:
                    descriptor = self.adapter.inspect_skill(path)
                except SkillValidationError as exc:
                    self._record_failure(catalog, path, str(exc))
                    continue
                name = descriptor.name
                if name in seen_in_root:
                    self._record_failure(
                        catalog,
                        path,
                        f"duplicate skill name {name!r} in root {root} "
                        f"(already defined by {seen_in_root[name]})",
                        name=name,
                    )
                    continue
                seen_in_root[name] = path
                if not descriptor.enabled:
                    catalog.disabled.append(name)
                    continue
                if name in winners:
                    if self.on_duplicate == "error":
                        self._record_failure(
                            catalog,
                            path,
                            f"duplicate skill name {name!r} already loaded from "
                            f"{winners[name].path}",
                            name=name,
                        )
                        continue
                    shadowed.setdefault(name, []).append(path)
                    continue
                winners[name] = LoadedSkill(
                    descriptor=descriptor, path=path, root=root.resolve()
                )
        catalog.skills = [
            LoadedSkill(
                descriptor=skill.descriptor,
                path=skill.path,
                root=skill.root,
                shadows=tuple(shadowed.get(name, ())),
            )
            for name, skill in winners.items()
        ]
        return catalog

    @staticmethod
    def _packages(root: Path) -> Iterable[Path]:
        """Yield candidate package directories in a stable order."""
        if not root.is_dir():
            return []
        return [
            path
            for path in sorted(root.iterdir(), key=lambda p: p.name)
            if path.is_dir() and (path / "skill.json").is_file()
        ]

    def _record_failure(
        self, catalog: SkillCatalog, path: Path, error: str, *, name: str | None = None
    ) -> None:
        if self.strict:
            raise SkillValidationError(f"{path}: {error}")
        catalog.failures.append(SkillLoadFailure(path=path, error=error, name=name))


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
        provider_id=None,
    ) -> ImportedSkill:
        """Import one directory skill without bypassing Phase 5C authority."""
        descriptor = adapter.inspect_skill(source)
        return self.import_descriptor(
            descriptor,
            adapter,
            source=source,
            allowed_permissions=allowed_permissions,
            provider_id=provider_id,
        )

    def import_catalog(
        self,
        catalog: SkillCatalog,
        *,
        allowed_permissions: tuple[str, ...] = (),
        adapter: DirectorySkillAdapter | None = None,
        provider_for: Callable[[SkillDescriptor], object] | None = None,
    ) -> list[ImportedSkill]:
        """Register a loaded catalog through the same safety pipeline.

        ``provider_for`` maps a Skill to an already-registered provider id —
        this is how a cognitive Skill is bound to an external Agent Runtime
        without the Skill itself naming an endpoint.  Returning ``None`` falls
        back to ``adapter``'s own per-skill provider.
        """
        imported = []
        for skill in catalog.list():
            provider_id = provider_for(skill.descriptor) if provider_for else None
            imported.append(
                self.import_descriptor(
                    skill.descriptor,
                    adapter,
                    source=skill.path,
                    allowed_permissions=allowed_permissions,
                    provider_id=provider_id,
                )
            )
        return imported

    def import_descriptor(
        self,
        descriptor: SkillDescriptor,
        adapter: DirectorySkillAdapter | None = None,
        *,
        source: str | Path,
        allowed_permissions: tuple[str, ...] = (),
        provider_id=None,
    ) -> ImportedSkill:
        """Register one inspected Skill as capabilities, definition and binding."""
        if provider_id is None and adapter is None:
            raise SkillValidationError(
                "a skill needs either an execution adapter or a registered provider"
            )
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
                "instructions": descriptor.instructions,
                "input_schema": dict(descriptor.input_schema),
                "output_schema": dict(descriptor.output_schema),
            },
            provides_capabilities=refs,
        )
        self.runtime.register_process(
            definition, external_provider_proxy, bind_internal=False
        )
        if provider_id is None:
            provider = self.runtime.register_provider(
                adapter.prepare_provider(descriptor), adapter
            )
        else:
            provider = self.runtime.provider_store.get_provider(provider_id)
            if provider is None:
                raise SkillValidationError(f"provider {provider_id} is not registered")
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
        reasons = [f"provider binding {binding.id} registered"]
        undeclared = requested - set(provider.declared_permissions)
        if undeclared:
            reasons.append(
                f"provider {provider.name} does not declare {sorted(undeclared)}; "
                "the binding stays ineligible until it does"
            )
        imported = ImportedSkill(
            source=str(Path(source).resolve()),
            descriptor=descriptor,
            status="ENABLED" if provider.operational and not undeclared else "UNAVAILABLE",
            provider_id=provider.id,
            process_definition_name=definition.name,
            process_definition_version=definition.version,
            reasons=reasons,
        )
        return self.runtime.provider_store.save_imported_skill(imported)
