"""Configuration for the Project Orchestrator and the Project Agent it uses.

Application wiring, like :mod:`nexus_seed.llm_config` and
:mod:`nexus_seed.providers` — it reads the environment and builds a
:class:`~nexus_seed.orchestrator.ProjectOrchestrator` whose Project Agents run
either in this process or in a real external Agent Runtime::

    NEXUS_SEED_PROJECT_AGENT_RUNTIME=a2a
    NEXUS_SEED_PROJECT_AGENT_URL=http://127.0.0.1:8801

The token-by-environment-variable rule is the existing external-agent setting,
reused rather than duplicated: no token is ever written to the database.

What a Project Agent may be *given* is configured here too, and closed by
default::

    NEXUS_SEED_PROJECT_RESOURCE_READ_ROOTS=/srv/shared:/srv/specs
    NEXUS_SEED_PROJECT_RESOURCE_WRITE_ROOTS=/srv/shared/out

With neither set there is no :class:`~nexus_seed.workspace.policy.GrantPolicy`
at all, so nothing outside the task's own workspace is grantable — NEXUS SEED
never hands an Agent the host it happens to be running on.

What Skills an external Project Agent has is *not* configured here. A Project
is delegated as a goal, not as a method: the Agent reads its own configuration,
and NEXUS SEED neither sends nor inventories its Skills.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .backends.base import ExecutionBackend
from .backends.llm import LLMBackend
from .llm_config import LLMSettings, load_env_file
from .orchestrator import (
    A2AAgentRuntime,
    AgentRuntime,
    InProcessAgentRuntime,
    ProjectOrchestrator,
)
from .providers.a2a import A2AEndpoint
from .providers.project_agent import A2AProjectAgentTransport
from .resources.scope import ResourceScope
from .storage.database import Database
from .workspace.policy import GrantPolicy

logger = logging.getLogger(__name__)

#: Where Project Agents run.  ``in_process`` keeps everything local and
#: network-free; ``a2a`` delegates each Project to an external Agent Runtime.
RUNTIMES = ("in_process", "a2a")

#: A Project Agent owns a whole Goal, so it is given far longer than one Skill
#: execution before the delegation is called unreachable.
DEFAULT_TIMEOUT_SECONDS = 900.0


class ProjectAgentConfigurationError(ValueError):
    """Project Agent settings are enabled but incomplete or invalid."""


@dataclass(frozen=True)
class ProjectAgentSettings:
    """Where Project Agents run and how they are reached."""

    runtime: str = "in_process"
    url: str = ""
    token_env: str | None = None
    poll_interval_seconds: float = 2.0
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    request_timeout_seconds: float = 30.0
    #: Workspace handed to a Project Agent, one directory per project.  Keep it
    #: relative unless the Agent Runtime can really write the absolute path.
    workspace_root: str | None = None
    #: Host directories a Project may be granted files from.  Empty means no
    #: host file is grantable at all.
    resource_read_roots: tuple[str, ...] = ()
    #: Of those, the ones a Project may also write back into.  Readable does
    #: not imply writable, which is why this is a separate list.
    resource_write_roots: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> ProjectAgentSettings:
        """Load Project Agent settings from ``env_file`` and the environment."""
        load_env_file(env_file)
        runtime = (
            os.environ.get("NEXUS_SEED_PROJECT_AGENT_RUNTIME", "in_process").strip().lower()
            or "in_process"
        )
        if runtime not in RUNTIMES:
            raise ProjectAgentConfigurationError(
                f"NEXUS_SEED_PROJECT_AGENT_RUNTIME must be one of {', '.join(RUNTIMES)}"
            )
        url = os.environ.get("NEXUS_SEED_PROJECT_AGENT_URL", "").strip()
        if runtime == "a2a" and not url:
            raise ProjectAgentConfigurationError(
                "NEXUS_SEED_PROJECT_AGENT_URL is required when "
                "NEXUS_SEED_PROJECT_AGENT_RUNTIME is 'a2a'"
            )
        token_env = os.environ.get("NEXUS_SEED_PROJECT_AGENT_TOKEN_ENV", "").strip() or None
        workspace = os.environ.get("NEXUS_SEED_PROJECT_WORKSPACE", "").strip() or None
        return cls(
            runtime=runtime,
            url=url,
            token_env=token_env,
            poll_interval_seconds=_read_positive_float(
                "NEXUS_SEED_PROJECT_AGENT_POLL_SECONDS", 2.0
            ),
            timeout_seconds=_read_positive_float(
                "NEXUS_SEED_PROJECT_AGENT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
            ),
            request_timeout_seconds=_read_positive_float(
                "NEXUS_SEED_PROJECT_AGENT_REQUEST_TIMEOUT_SECONDS", 30.0
            ),
            workspace_root=workspace,
            resource_read_roots=_read_roots("NEXUS_SEED_PROJECT_RESOURCE_READ_ROOTS"),
            resource_write_roots=_read_roots("NEXUS_SEED_PROJECT_RESOURCE_WRITE_ROOTS"),
        )

    def endpoint(self) -> A2AEndpoint:
        """Return the A2A connection settings for the Project Agent runtime."""
        try:
            return A2AEndpoint(
                url=self.url,
                token_env=self.token_env,
                poll_interval_seconds=self.poll_interval_seconds,
                timeout_seconds=self.timeout_seconds,
                request_timeout_seconds=self.request_timeout_seconds,
            )
        except ValueError as exc:
            raise ProjectAgentConfigurationError(f"project agent: {exc}") from exc


def build_agent_runtime(settings: ProjectAgentSettings) -> AgentRuntime:
    """Build the AgentRuntime the settings ask for.

    The orchestrator is identical either way — swapping the runtime is the
    whole point of the interface, so this is the only place that chooses.
    """
    if settings.runtime != "a2a":
        return InProcessAgentRuntime()
    return A2AAgentRuntime(A2AProjectAgentTransport(settings.endpoint()))


def build_routing_backend(env_file: str | Path = ".env") -> ExecutionBackend | None:
    """Return the reasoning backend the ProjectRouter should use, if any.

    Without one the router deterministically creates a project for every
    request; it never falls back to matching words against existing goals.
    """
    settings = LLMSettings.from_env(env_file)
    if not settings.enabled:
        return None
    return LLMBackend(
        provider=settings.provider,
        model=settings.model,
        api_key_env=settings.api_key_env,
        max_tokens=settings.max_tokens,
        base_url=settings.base_url,
        timeout_seconds=settings.timeout_seconds,
    )


def build_grant_policy(
    settings: ProjectAgentSettings,
    *,
    resources: object | None = None,
    ledger: object | None = None,
) -> GrantPolicy | None:
    """Build the policy deciding what a Project may be given, or ``None``.

    ``None`` is the honest answer when nothing is authorized: the orchestrator
    then refuses every request with "no grant policy is configured", which is
    the behaviour that predates grants rather than a silently open host.

    The roots are not created if they are missing.  An operator naming a
    directory that does not exist has made a mistake, and quietly making an
    empty one at that path would hide it.
    """
    if not settings.resource_read_roots and resources is None and ledger is None:
        return None
    scope = None
    if settings.resource_read_roots:
        scope = ResourceScope(
            read_roots=list(settings.resource_read_roots),
            write_roots=list(settings.resource_write_roots),
            create=False,
        )
        logger.info(
            "project resource grants: %d readable root(s), %d writable",
            len(settings.resource_read_roots),
            len(settings.resource_write_roots),
        )
    return GrantPolicy(scope=scope, resources=resources, ledger=ledger)  # type: ignore[arg-type]


def build_orchestrator(
    db_path: str | Path | Database,
    *,
    env_file: str | Path = ".env",
    settings: ProjectAgentSettings | None = None,
    backend: ExecutionBackend | None = None,
    grant_policy: GrantPolicy | None = None,
) -> ProjectOrchestrator:
    """Build a configured :class:`ProjectOrchestrator` over ``db_path``."""
    settings = settings or ProjectAgentSettings.from_env(env_file)
    runtime = build_agent_runtime(settings)
    logger.info("project orchestrator: %s agent runtime", runtime.name)
    return ProjectOrchestrator(
        db_path if isinstance(db_path, Database) else str(db_path),
        agent_runtime=runtime,
        backend=backend if backend is not None else build_routing_backend(env_file),
        workspace_root=settings.workspace_root,
        grant_policy=grant_policy if grant_policy is not None else build_grant_policy(settings),
    )


def _read_roots(name: str) -> tuple[str, ...]:
    """Read one ``os.pathsep``-separated list of directories from the environment."""
    raw = os.environ.get(name, "")
    return tuple(part.strip() for part in raw.split(os.pathsep) if part.strip())


def _read_positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ProjectAgentConfigurationError(f"{name} must be a number") from exc
    if value <= 0:
        raise ProjectAgentConfigurationError(f"{name} must be greater than zero")
    return value


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ProjectAgentConfigurationError",
    "ProjectAgentSettings",
    "RUNTIMES",
    "build_agent_runtime",
    "build_grant_policy",
    "build_orchestrator",
    "build_routing_backend",
]
