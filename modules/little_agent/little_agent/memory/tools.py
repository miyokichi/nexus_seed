"""Tools that write persistent memory.

They are supplied by the store rather than registered by the runtime, so an
agent with a :class:`~little_agent.memory.store.NullMemoryStore` is not merely
denied them — they do not exist for it.
"""

from __future__ import annotations

from little_agent.memory.store import MemoryFile
from little_agent.tools.base import ToolContext, ToolResult

_PARAMETERS = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "Full markdown content to save (replaces what is there).",
        },
    },
    "required": ["content"],
    "additionalProperties": False,
}


class UpdateWorkspaceMemoryTool:
    name = "update_workspace_memory"
    description = (
        "Overwrite the workspace-level persistent memory (memory.md). "
        "Use to remember facts, preferences, or context specific to this workspace."
    )
    requires_confirmation = False
    parameters = _PARAMETERS

    def __init__(self, memory: MemoryFile) -> None:
        self._memory = memory

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        self._memory.save(str(kwargs["content"]))
        return ToolResult(True, f"Workspace memory saved to {self._memory.path}")


class UpdateGlobalMemoryTool:
    name = "update_global_memory"
    description = (
        "Overwrite the global persistent memory shared across all workspaces and agents. "
        "Use to remember facts, preferences, or context that apply everywhere."
    )
    requires_confirmation = False
    parameters = _PARAMETERS

    def __init__(self, memory: MemoryFile) -> None:
        self._memory = memory

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        self._memory.save(str(kwargs["content"]))
        return ToolResult(True, f"Global memory saved to {self._memory.path}")
