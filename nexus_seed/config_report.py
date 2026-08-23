"""What NEXUS SEED is actually configured to do, in one place.

Two execution locations are involved in a running system, and their settings
can look alike:

* **NEXUS SEED's own reasoning** (``NEXUS_SEED_LLM_*``) — routing a message to
  a Project, evaluating Knowledge, and answering a question about one.  This is
  the model NEXUS SEED thinks with.
* **The Project Agent's model** — the one that does the delegated work.  It is
  *not configured here at all*: NEXUS SEED says where the Agent Runtime is
  (``NEXUS_SEED_PROJECT_AGENT_*``) and hands it the Project; the Agent brings
  its own model, its own keys, its own Skills, and its own configuration file.

That distinction is easy to miss in a flat ``.env``, so this module reports the
resolved settings grouped by what they are for.  It reads only; it starts
nothing and connects to nothing.

Secrets are never printed.  An API key is named by the *variable that holds
it*, so the report says which variable is consulted and whether it currently
has a value — never the value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any

from .llm_config import LLMSettings, load_env_file
from .orchestrator_config import ProjectAgentSettings


@dataclass(frozen=True, slots=True)
class ConfigGroup:
    """One labelled group of resolved settings."""

    name: str
    purpose: str
    settings: list[tuple[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "settings": dict(self.settings),
            "notes": list(self.notes),
        }


def build_report(env_file: str | Path = ".env") -> dict[str, Any]:
    """Return the resolved configuration, grouped by what each part is for."""

    resolved = load_env_file(env_file)
    groups = [
        _reasoning_group(env_file),
        _delegation_group(env_file),
    ]
    return {
        "env_file": str(resolved) if resolved else None,
        "groups": [group.to_dict() for group in groups],
    }


def _reasoning_group(env_file) -> ConfigGroup:
    """The model NEXUS SEED thinks with."""

    settings = LLMSettings.from_env(env_file)
    notes = [
        "Used by: routing a message to a Project, answering a question about "
        "one, evaluating Knowledge, and driving an in-process Project Agent.",
    ]
    if not settings.enabled:
        notes.append(
            "Disabled. NEXUS SEED still runs: a message is explained rather "
            "than routed unless it plainly asks for something to happen."
        )
    return ConfigGroup(
        name="NEXUS SEED's own reasoning",
        purpose="the model NEXUS SEED thinks with (NEXUS_SEED_LLM_*)",
        settings=[
            ("NEXUS_SEED_LLM_ENABLED", settings.enabled),
            ("NEXUS_SEED_LLM_PROVIDER", settings.provider),
            ("NEXUS_SEED_LLM_MODEL", settings.model),
            ("NEXUS_SEED_LLM_BASE_URL", settings.base_url or "(provider default)"),
            ("NEXUS_SEED_LLM_MAX_TOKENS", settings.max_tokens),
            ("NEXUS_SEED_LLM_TIMEOUT_SECONDS", settings.timeout_seconds),
            ("NEXUS_SEED_LLM_API_KEY_ENV", settings.api_key_env),
            (
                f"  {settings.api_key_env}",
                "set" if os.environ.get(settings.api_key_env, "").strip() else "EMPTY",
            ),
        ],
        notes=notes,
    )


def _delegation_group(env_file) -> ConfigGroup:
    """Where the work is delegated to — not what model does it."""

    settings = ProjectAgentSettings.from_env(env_file)
    notes = [
        "This says WHERE a Project is delegated, not how the work is done. "
        "The Agent Runtime brings its own model, keys and Skills, from its own "
        "configuration file; nothing here configures or reads them.",
    ]
    rows: list[tuple[str, Any]] = [
        ("NEXUS_SEED_PROJECT_AGENT_RUNTIME", settings.runtime),
    ]
    if settings.runtime == "in_process":
        notes.append(
            "in_process is the local, network-free Agent Runtime. It reuses "
            "NEXUS SEED's reasoning backend and cannot invoke external tools."
        )
    else:
        rows.extend(
            [
                ("NEXUS_SEED_PROJECT_AGENT_URL", settings.url),
                (
                    "NEXUS_SEED_PROJECT_AGENT_TOKEN_ENV",
                    settings.token_env or "(no token)",
                ),
            ]
        )
        if settings.token_env:
            rows.append(
                (
                    f"  {settings.token_env}",
                    "set" if os.environ.get(settings.token_env, "").strip() else "EMPTY",
                )
            )
        rows.extend(
            [
                ("NEXUS_SEED_PROJECT_AGENT_TIMEOUT_SECONDS", settings.timeout_seconds),
                ("NEXUS_SEED_PROJECT_AGENT_POLL_SECONDS", settings.poll_interval_seconds),
                (
                    "NEXUS_SEED_PROJECT_AGENT_REQUEST_TIMEOUT_SECONDS",
                    settings.request_timeout_seconds,
                ),
            ]
        )
    rows.append(
        ("NEXUS_SEED_PROJECT_WORKSPACE", settings.workspace_root or "(none)")
    )
    return ConfigGroup(
        name="Delegation",
        purpose="where a Project is handed over (NEXUS_SEED_PROJECT_AGENT_*)",
        settings=rows,
        notes=notes,
    )


def format_report(report: dict[str, Any]) -> str:
    """Render the report for a terminal."""

    lines = [
        f"env file: {report['env_file'] or '(none found)'}",
        "",
    ]
    for group in report["groups"]:
        lines.append(f"[{group['name']}]")
        lines.append(f"  {group['purpose']}")
        width = max((len(name) for name in group["settings"]), default=0)
        for name, value in group["settings"].items():
            lines.append(f"    {name.ljust(width)}  {value}")
        for note in group["notes"]:
            lines.append(f"  - {note}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["ConfigGroup", "build_report", "format_report"]
