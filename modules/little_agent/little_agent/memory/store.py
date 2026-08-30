"""What an agent remembers between runs, behind one small interface.

The runtime holds a :class:`MemoryStore` and asks it three things:

``sections()``
    Prompt sections to place in the system prompt (what is already known).
``tools()``
    Tools the agent may call to write something down.
``learn(...)``
    Distil the session that just happened into durable profiles.

:class:`NullMemoryStore` answers "nothing" to all three, which is exactly what a
``--no-memory`` chat and every A2A task want. :class:`FileMemoryStore` keeps four
markdown files: workspace and global *memory* (written deliberately, by the agent
or by hand) and workspace and global *profile* (distilled automatically).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from little_agent.memory.reflection import Reflector

if TYPE_CHECKING:
    from little_agent.llm import LLMClient
    from little_agent.messages import Message
    from little_agent.tools.base import Tool


@dataclass(frozen=True, slots=True)
class MemoryFile:
    """One markdown memory file. Missing file reads as empty text."""

    path: Path

    def load(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    def save(self, content: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(content, encoding="utf-8")


@runtime_checkable
class MemoryStore(Protocol):
    """Persistent memory as the runtime sees it."""

    #: False for a store that keeps nothing, so callers can skip memory UI.
    enabled: bool

    def sections(self) -> list[tuple[str, str]]:
        """``(heading, body)`` pairs to add to the system prompt."""

    def tools(self) -> list["Tool"]:
        """Tools that let the agent write to this memory."""

    def learn(self, messages: list["Message"], llm: "LLMClient", model: str) -> bool:
        """Distil ``messages`` into durable profiles. True when something changed."""

    def describe(self) -> str:
        """One-line description for ``/config`` and the CLI banner."""


class NullMemoryStore:
    """Remembers nothing: no prompt sections, no tools, no learning.

    Used by ``chat --no-memory`` and by every A2A task, so a served agent never
    reads or writes a user's durable memory.
    """

    enabled = False

    def sections(self) -> list[tuple[str, str]]:
        return []

    def tools(self) -> list["Tool"]:
        return []

    def learn(self, messages: list["Message"], llm: "LLMClient", model: str) -> bool:
        return False

    def describe(self) -> str:
        return "off (nothing is persisted)"


@dataclass(slots=True)
class FileMemoryStore:
    """Markdown files on disk: deliberate memory plus auto-learned profiles.

    ``auto_learning`` gates :meth:`learn` only; the memory files are always read
    and always writable through the tools.
    """

    workspace_memory: MemoryFile
    global_memory: MemoryFile
    workspace_profile: MemoryFile
    global_profile: MemoryFile
    auto_learning: bool = True
    enabled: bool = field(default=True, init=False)

    @classmethod
    def from_config(cls, config: Any) -> "FileMemoryStore":
        return cls(
            workspace_memory=MemoryFile(config.workspace / "memory.md"),
            global_memory=MemoryFile(config.global_memory_path),
            workspace_profile=MemoryFile(config.workspace / "profile.md"),
            global_profile=MemoryFile(config.global_profile_path),
            auto_learning=config.enable_auto_learning,
        )

    def sections(self) -> list[tuple[str, str]]:
        pairs = (
            ("Global Memory", self.global_memory),
            ("Workspace Memory", self.workspace_memory),
            ("User Profile (learned)", self.global_profile),
            ("Workspace Profile (learned)", self.workspace_profile),
        )
        return [(heading, body) for heading, memory in pairs if (body := memory.load())]

    def tools(self) -> list["Tool"]:
        # Imported here so the store module stays importable from the tool layer.
        from little_agent.memory.tools import UpdateGlobalMemoryTool, UpdateWorkspaceMemoryTool

        return [
            UpdateWorkspaceMemoryTool(self.workspace_memory),
            UpdateGlobalMemoryTool(self.global_memory),
        ]

    def learn(self, messages: list["Message"], llm: "LLMClient", model: str) -> bool:
        if not self.auto_learning or not messages:
            return False
        # A rule-based fallback client cannot summarize; don't ask it to.
        from little_agent.llm import LocalRuleClient

        if isinstance(llm, LocalRuleClient):
            return False
        result = Reflector(llm, model).reflect(
            messages, self.global_profile.load(), self.workspace_profile.load()
        )
        if result is None:
            return False
        new_global, new_workspace = result
        # An empty string means "leave the existing profile untouched".
        if new_global:
            self.global_profile.save(new_global)
        if new_workspace:
            self.workspace_profile.save(new_workspace)
        return bool(new_global or new_workspace)

    def describe(self) -> str:
        learning = "on" if self.auto_learning else "off"
        return (
            f"on (workspace: {self.workspace_memory.path}, "
            f"global: {self.global_memory.path}, auto-learning: {learning})"
        )
