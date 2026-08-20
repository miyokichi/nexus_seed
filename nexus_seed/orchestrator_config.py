"""Configuration for the Project Orchestrator and the Project Agent it uses.

Application wiring, like :mod:`nexus_seed.llm_config` and
:mod:`nexus_seed.federation_config` — it reads the environment and builds a
:class:`~nexus_seed.orchestrator.ProjectOrchestrator` whose Project Agents run
either in this process or in a real external Agent Runtime::

    NEXUS_SEED_PROJECT_AGENT_RUNTIME=a2a
    NEXUS_SEED_PROJECT_AGENT_URL=http://127.0.0.1:8801

Skill roots and the token-by-environment-variable rule are the existing
external-agent settings, reused rather than duplicated: a Skill still never
names an endpoint, and no token is ever written to the database.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .backends.base import ExecutionBackend
from .backends.llm import LLMBackend
from .llm_config import LLMSettings, load_env_file
from .skills_config import SkillSettings
from .orchestrator import (
    A2AAgentRuntime,
    AgentRuntime,
    InProcessAgentRuntime,
    ProjectOrchestrator,
)
from .providers.a2a import A2AEndpoint
from .providers.project_agent import A2AProjectAgentTransport, skill_contracts
from .providers.skills import SkillCatalog
from .storage.database import Database

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
    #: Where the Skills offered to a Project Agent come from.  Not an Agent
    #: setting: the same catalog is offered to every executor.
    skills: SkillSettings = field(default_factory=SkillSettings)

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
        # A Project Agent is offered the same Skills any other executor would
        # be, from the one place they are configured.
        skills = SkillSettings.from_env(env_file)
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
            skills=skills,
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


def load_skills(settings: ProjectAgentSettings) -> SkillCatalog:
    """Load the Skills a Project Agent may choose between.

    One broken package is reported and skipped: a Project Agent decides for
    itself which procedures apply, so a missing one narrows its choice rather
    than stopping the orchestrator from running.
    """
    catalog = settings.skills.load()
    for failure in catalog.failures:
        logger.error("skill package rejected: %s", failure)
    return catalog


def build_agent_runtime(
    settings: ProjectAgentSettings, *, catalog: SkillCatalog | None = None
) -> AgentRuntime:
    """Build the AgentRuntime the settings ask for.

    The orchestrator is identical either way — swapping the runtime is the
    whole point of the interface, so this is the only place that chooses.
    """
    if settings.runtime != "a2a":
        return InProcessAgentRuntime()
    catalog = catalog if catalog is not None else load_skills(settings)
    transport = A2AProjectAgentTransport(
        settings.endpoint(), skills=skill_contracts(catalog)
    )
    return A2AAgentRuntime(transport)


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


def build_orchestrator(
    db_path: str | Path | Database,
    *,
    env_file: str | Path = ".env",
    settings: ProjectAgentSettings | None = None,
    backend: ExecutionBackend | None = None,
) -> ProjectOrchestrator:
    """Build a configured :class:`ProjectOrchestrator` over ``db_path``."""
    settings = settings or ProjectAgentSettings.from_env(env_file)
    catalog = load_skills(settings)
    runtime = build_agent_runtime(settings, catalog=catalog)
    logger.info(
        "project orchestrator: %s agent runtime, %d skill(s) offered",
        runtime.name,
        len(catalog.list()),
    )
    return ProjectOrchestrator(
        db_path if isinstance(db_path, Database) else str(db_path),
        agent_runtime=runtime,
        backend=backend if backend is not None else build_routing_backend(env_file),
        workspace_root=settings.workspace_root,
        available_skills=tuple(skill.name for skill in catalog.list()),
    )


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
    "build_orchestrator",
    "build_routing_backend",
    "load_skills",
]
