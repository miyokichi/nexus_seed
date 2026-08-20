"""Configuration for external Agent Runtimes and the Skills routed to them.

This is application wiring, not Runtime mechanism — the same role
:mod:`nexus_seed.llm_config` plays for the LLM boundary.  It reads a small JSON
file plus a few environment variables, then registers:

* one :class:`~nexus_seed.providers.models.ExecutionProvider` per remote A2A
  agent, with an :class:`~nexus_seed.providers.a2a.A2AAgentAdapter`;
* every Skill package found under the configured roots, bound to the provider
  its capability is routed to.

The three concerns stay separate on purpose::

    Skill      how to think        skills/<name>/SKILL.md
    Capability what can be done    capability registry
    Provider   where it runs       providers + bindings in this config

JSON rather than YAML because ``json-repair`` is this project's only runtime
dependency and the standard library reads JSON.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .llm_config import load_env_file
from .providers.a2a import A2AAgentAdapter, A2AEndpoint, A2AProtocolError, a2a_provider_record
from .providers.skills import SkillCatalog, SkillImporter, SkillLoader
from .skills_config import DEFAULT_SKILL_ROOTS as _DEFAULT_SKILL_ROOTS, read_roots

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .runtime.runtime import Runtime

#: Re-exported so an existing import keeps working; Skill discovery is owned
#: by :mod:`nexus_seed.skills_config` now.
DEFAULT_SKILL_ROOTS = _DEFAULT_SKILL_ROOTS


class FederationConfigurationError(ValueError):
    """External agent settings are enabled but incomplete or invalid."""


@dataclass(frozen=True)
class A2AProviderSettings:
    """One remote A2A agent as configured by the operator."""

    name: str
    url: str
    token_env: str | None = None
    poll_interval_seconds: float = 1.0
    timeout_seconds: float = 300.0
    request_timeout_seconds: float = 30.0
    priority: int = 0
    trust_level: float = 0.5
    permissions: tuple[str, ...] = ()
    verify_agent_card: bool = False

    def endpoint(self) -> A2AEndpoint:
        """Return the connection settings for this provider."""
        try:
            return A2AEndpoint(
                url=self.url,
                token_env=self.token_env,
                poll_interval_seconds=self.poll_interval_seconds,
                timeout_seconds=self.timeout_seconds,
                request_timeout_seconds=self.request_timeout_seconds,
            )
        except ValueError as exc:
            raise FederationConfigurationError(f"provider {self.name}: {exc}") from exc


@dataclass(frozen=True)
class FederationSettings:
    """Validated provider, binding and Skill-root settings."""

    enabled: bool = False
    providers: tuple[A2AProviderSettings, ...] = ()
    #: capability or skill name -> provider name
    bindings: dict = field(default_factory=dict)
    skill_roots: tuple[str, ...] = DEFAULT_SKILL_ROOTS
    default_provider: str | None = None
    strict_skills: bool = False
    on_duplicate_skill: str = "override"
    allowed_permissions: tuple[str, ...] = ()

    @classmethod
    def from_env(
        cls, env_file: str | Path = ".env", *, config_file: str | Path | None = None
    ) -> FederationSettings:
        """Load settings from ``env_file`` and the JSON config it points at."""
        load_env_file(env_file)
        enabled = _read_bool("NEXUS_SEED_A2A_ENABLED", default=False)
        path = config_file or os.environ.get("NEXUS_SEED_A2A_CONFIG", "").strip()
        raw: dict = {}
        if path:
            config_path = Path(path).expanduser()
            if not config_path.is_file():
                raise FederationConfigurationError(
                    f"NEXUS_SEED_A2A_CONFIG does not exist: {config_path}"
                )
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise FederationConfigurationError(
                    f"invalid A2A config {config_path}: {exc}"
                ) from exc
            if not isinstance(raw, dict):
                raise FederationConfigurationError("A2A config must be a JSON object")
        return cls.from_dict(raw, enabled=enabled)

    @classmethod
    def from_dict(cls, raw: dict, *, enabled: bool = True) -> FederationSettings:
        """Build settings from an already-parsed configuration object."""
        providers = []
        for name, entry in (raw.get("providers") or {}).items():
            if not isinstance(entry, dict):
                raise FederationConfigurationError(
                    f"provider {name} must be a JSON object"
                )
            kind = str(entry.get("type") or "a2a").lower()
            if kind != "a2a":
                raise FederationConfigurationError(
                    f"provider {name}: unsupported type {kind!r} (only 'a2a')"
                )
            url = str(entry.get("url") or "").strip()
            if not url:
                raise FederationConfigurationError(f"provider {name} needs a url")
            providers.append(
                A2AProviderSettings(
                    name=str(name),
                    url=url,
                    token_env=(str(entry["token_env"]) if entry.get("token_env") else None),
                    poll_interval_seconds=float(entry.get("poll_interval_seconds", 1.0)),
                    timeout_seconds=float(entry.get("timeout_seconds", 300.0)),
                    request_timeout_seconds=float(
                        entry.get("request_timeout_seconds", 30.0)
                    ),
                    priority=int(entry.get("priority", 0)),
                    trust_level=float(entry.get("trust_level", 0.5)),
                    permissions=tuple(entry.get("permissions") or ()),
                    verify_agent_card=bool(entry.get("verify_agent_card", False)),
                )
            )
        provider_names = {provider.name for provider in providers}

        bindings = {}
        for key, entry in (raw.get("bindings") or {}).items():
            target = entry.get("provider") if isinstance(entry, dict) else entry
            target = str(target or "").strip()
            if target not in provider_names:
                raise FederationConfigurationError(
                    f"binding {key!r} names unknown provider {target!r}"
                )
            bindings[str(key)] = target

        skills = raw.get("skills") or {}
        if not isinstance(skills, dict):
            raise FederationConfigurationError("skills must be a JSON object")
        roots = tuple(str(root) for root in (skills.get("roots") or ())) or _env_roots()
        on_duplicate = str(skills.get("on_duplicate") or "override")
        if on_duplicate not in {"override", "error"}:
            raise FederationConfigurationError(
                "skills.on_duplicate must be 'override' or 'error'"
            )
        default_provider = raw.get("default_provider")
        if default_provider and str(default_provider) not in provider_names:
            raise FederationConfigurationError(
                f"default_provider names unknown provider {default_provider!r}"
            )
        return cls(
            enabled=enabled,
            providers=tuple(providers),
            bindings=bindings,
            skill_roots=roots,
            default_provider=str(default_provider) if default_provider else None,
            strict_skills=bool(skills.get("strict", False)),
            on_duplicate_skill=on_duplicate,
            allowed_permissions=tuple(skills.get("allowed_permissions") or ()),
        )


@dataclass
class FederationReport:
    """What one configuration pass actually registered, for operators."""

    providers: dict = field(default_factory=dict)
    catalog: SkillCatalog = field(default_factory=SkillCatalog)
    imported: list = field(default_factory=list)
    unroutable: list = field(default_factory=list)
    agent_cards: dict = field(default_factory=dict)
    card_errors: dict = field(default_factory=dict)


def configure_external_agents(
    runtime: Runtime,
    *,
    env_file: str | Path = ".env",
    config_file: str | Path | None = None,
    settings: FederationSettings | None = None,
) -> FederationReport | None:
    """Register configured A2A providers and load the Skills bound to them.

    Returns ``None`` when the integration is disabled, leaving every existing
    deterministic and internal execution path exactly as it was.
    """
    settings = settings or FederationSettings.from_env(env_file, config_file=config_file)
    if not settings.enabled:
        return None

    report = FederationReport()
    for provider_settings in settings.providers:
        endpoint = provider_settings.endpoint()
        adapter = A2AAgentAdapter(endpoint)
        card = None
        if provider_settings.verify_agent_card:
            try:
                card = adapter.client.agent_card()
                report.agent_cards[provider_settings.name] = card.to_dict()
            except A2AProtocolError as exc:
                # A missing card means the provider is unavailable; it never
                # means a Capability is missing.
                report.card_errors[provider_settings.name] = str(exc)
        provider = runtime.register_provider(
            a2a_provider_record(
                provider_settings.name,
                endpoint,
                declared_permissions=provider_settings.permissions,
                priority=provider_settings.priority,
                trust_level=provider_settings.trust_level,
                agent_card=card,
            ),
            adapter,
        )
        report.providers[provider_settings.name] = provider

    catalog = SkillLoader(
        settings.skill_roots,
        strict=settings.strict_skills,
        on_duplicate=settings.on_duplicate_skill,
    ).load()
    report.catalog = catalog

    def provider_for(descriptor):
        name = _routed_provider(descriptor, settings)
        if name is None:
            return None
        provider = report.providers.get(name)
        return provider.id if provider else None

    importer = SkillImporter(runtime)
    for skill in catalog.list():
        provider_id = provider_for(skill.descriptor)
        if provider_id is None:
            # A Skill with nowhere to run is recorded, not silently dropped:
            # the need is real even when no executor is configured.
            report.unroutable.append(skill.name)
            continue
        report.imported.append(
            importer.import_descriptor(
                skill.descriptor,
                source=skill.path,
                allowed_permissions=settings.allowed_permissions,
                provider_id=provider_id,
            )
        )
    return report


def _routed_provider(descriptor, settings: FederationSettings) -> str | None:
    """Resolve which provider executes a Skill: capability first, then name."""
    for claim in descriptor.provided_capabilities:
        name = str(claim.get("name") or "")
        if name in settings.bindings:
            return settings.bindings[name]
    if descriptor.name in settings.bindings:
        return settings.bindings[descriptor.name]
    return settings.default_provider


def _env_roots() -> tuple[str, ...]:
    return read_roots()


def _read_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise FederationConfigurationError(
        f"{name} must be one of: true/false, yes/no, on/off, 1/0"
    )


__all__ = [
    "A2AProviderSettings",
    "DEFAULT_SKILL_ROOTS",
    "FederationConfigurationError",
    "FederationReport",
    "FederationSettings",
    "configure_external_agents",
]
