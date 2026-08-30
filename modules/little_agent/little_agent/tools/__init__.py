from little_agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from little_agent.tools.delegation import DelegateTasksTool, DelegateTaskTool
from little_agent.tools.filesystem import (
    AppendFileTool,
    DeleteFileTool,
    ListDirTool,
    MoveFileTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from little_agent.tools.shell import RunPowerShellTool
from little_agent.tools.web import FetchUrlTool


def default_tools(enabled: set[str] | None = None) -> ToolRegistry:
    """Build the core tool registry.

    ``enabled`` is an optional allowlist of tool names; when given, only core
    tools whose name is in the set are registered. ``None`` registers all of
    them (the default).
    """

    registry = ToolRegistry()
    for tool in [
        ListDirTool(),
        ReadFileTool(),
        WriteFileTool(),
        AppendFileTool(),
        DeleteFileTool(),
        MoveFileTool(),
        SearchFilesTool(),
        RunPowerShellTool(),
        FetchUrlTool(),
    ]:
        if enabled is None or tool.name in enabled:
            registry.register(tool)
    return registry


__all__ = [
    "AppendFileTool",
    "DelegateTaskTool",
    "DelegateTasksTool",
    "DeleteFileTool",
    "FetchUrlTool",
    "MoveFileTool",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "default_tools",
]
