"""Where NEXUS SEED looks for **its own** Skills, and how it resolves duplicates.

These are the procedures NEXUS SEED itself can carry out: they are imported as
Processes and become its capabilities.  They are configured here, in NEXUS
SEED's own ``.env``::

    NEXUS_SEED_SKILL_ROOTS=./skills;C:/team/skills

They are *not* the Project Agent's skills.  A Project is delegated as a goal,
not as a method, so the Agent that works on it reads its skills from its own
configuration file.  NEXUS SEED does not send them, does not read them, and
does not know what they are.  Two agents, two skill sets, two config files.

Precedence is positional: the first root wins, so the conventional order is
project-local, then user/global, then anything shared.  The separator is the
platform's ``os.pathsep`` — ``;`` on Windows, ``:`` elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .llm_config import load_env_file
from .providers.skills import SkillCatalog, SkillLoader


#: Roots searched when nothing is configured, highest precedence first.
DEFAULT_SKILL_ROOTS = ("./skills", "~/.nexus_seed/skills")

#: How a name found in two roots is resolved.
DUPLICATE_POLICIES = ("override", "error")


class SkillConfigurationError(ValueError):
    """Skill settings are present but invalid."""


@dataclass(frozen=True, slots=True)
class SkillSettings:
    """Validated Skill discovery settings."""

    roots: tuple[str, ...] = DEFAULT_SKILL_ROOTS
    #: Whether one broken Skill package stops the load instead of being skipped.
    strict: bool = False
    #: ``override`` lets an earlier root win; ``error`` refuses to guess.
    on_duplicate: str = "override"

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> SkillSettings:
        """Load Skill settings from ``env_file`` and the environment."""

        load_env_file(env_file)
        return cls(
            roots=read_roots(),
            strict=_read_bool("NEXUS_SEED_SKILLS_STRICT", default=False),
            on_duplicate=_read_duplicate_policy(),
        )

    def load(self) -> SkillCatalog:
        """Scan the configured roots and return what was found."""

        return SkillLoader(
            self.roots, strict=self.strict, on_duplicate=self.on_duplicate
        ).load()

    def described_roots(self) -> list[tuple[str, bool]]:
        """Each root with whether it currently exists, for `nexus-seed config`."""

        return [(root, Path(root).expanduser().is_dir()) for root in self.roots]


def read_roots() -> tuple[str, ...]:
    """Return the configured roots, or the defaults when unset."""

    raw = os.environ.get("NEXUS_SEED_SKILL_ROOTS", "").strip()
    if not raw:
        return DEFAULT_SKILL_ROOTS
    roots = tuple(part.strip() for part in raw.split(os.pathsep) if part.strip())
    return roots or DEFAULT_SKILL_ROOTS


def _read_duplicate_policy() -> str:
    value = os.environ.get("NEXUS_SEED_SKILLS_ON_DUPLICATE", "").strip().lower()
    if not value:
        return "override"
    if value not in DUPLICATE_POLICIES:
        raise SkillConfigurationError(
            "NEXUS_SEED_SKILLS_ON_DUPLICATE must be "
            f"{' or '.join(DUPLICATE_POLICIES)}"
        )
    return value


def _read_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SkillConfigurationError(
        f"{name} must be one of: true/false, yes/no, on/off, 1/0"
    )


__all__ = [
    "DEFAULT_SKILL_ROOTS",
    "DUPLICATE_POLICIES",
    "SkillConfigurationError",
    "SkillSettings",
    "read_roots",
]
