"""Thin channel mappings; all command logic remains in ConsoleService."""

from __future__ import annotations

import json
import shlex


class DiscordSlashCommandMapper:
    """Map Discord-neutral slash fields to the same explicit command text.

    A future Discord transport can pass ``name``, ``subcommand`` and option
    values here without making Discord objects the domain source of truth.
    """

    @staticmethod
    def to_text(name: str, *, subcommand: str | None = None, options: dict | None = None) -> str:
        parts = [f"/{name.lstrip('/')}"]
        if subcommand:
            parts.append(shlex.quote(subcommand))
        for key, value in (options or {}).items():
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if isinstance(value, (dict, list, bool)) else str(value)
            parts.append(f"{key}={shlex.quote(encoded)}")
        return " ".join(parts)


class CLICommandMapper:
    """Identity mapper documenting that CLI uses the same command grammar."""

    @staticmethod
    def to_text(text: str) -> str:
        return text


__all__ = ["CLICommandMapper", "DiscordSlashCommandMapper"]
