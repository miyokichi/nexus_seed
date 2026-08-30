from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    when_to_use: str
    allowed_tools: list[str] = field(default_factory=list)
    instructions: str = ""
    path: Path | None = None

    def as_prompt(self) -> str:
        tools = ", ".join(self.allowed_tools) if self.allowed_tools else "(not specified)"
        return (
            f"# Skill: {self.name}\n"
            f"Description: {self.description}\n"
            f"When to use: {self.when_to_use}\n"
            f"Allowed tools: {tools}\n"
            f"Instructions:\n{self.instructions}"
        )
