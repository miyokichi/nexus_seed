from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from little_agent.paths import is_within, resolve_existing_parent


@dataclass(frozen=True, slots=True)
class PathAccessPolicy:
    workspace: Path
    readable_paths: tuple[Path, ...] = ()
    writable_paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", self.workspace.resolve())
        object.__setattr__(
            self,
            "readable_paths",
            tuple(path.resolve() for path in self.readable_paths),
        )
        object.__setattr__(
            self,
            "writable_paths",
            tuple(path.resolve() for path in self.writable_paths),
        )

    @property
    def readable_roots(self) -> tuple[Path, ...]:
        return (self.workspace, *self.readable_paths, *self.writable_paths)

    @property
    def writable_roots(self) -> tuple[Path, ...]:
        return (self.workspace, *self.writable_paths)

    def resolve(self, requested_path: str, access: str = "read") -> Path:
        path = Path(requested_path)
        if not path.is_absolute():
            path = self.workspace / path

        resolved = path.resolve()
        roots = self.readable_roots if access == "read" else self.writable_roots
        if any(is_within(root, resolved) for root in roots):
            return resolved

        if access == "write" and not path.exists():
            parent = resolve_existing_parent(path.parent)
            if parent.is_dir() and any(is_within(root, parent) for root in roots):
                return resolved

        raise ValueError(f"Path is outside allowed {access} paths: {requested_path}")

    def display(self, path: Path) -> str:
        resolved = path.resolve()
        for root in self.writable_roots + self.readable_roots:
            if is_within(root, resolved):
                try:
                    return str(resolved.relative_to(root))
                except ValueError:
                    continue
        return str(resolved)


@dataclass(frozen=True, slots=True)
class ToolContext:
    workspace: Path
    readable_paths: tuple[Path, ...] = field(default_factory=tuple)
    writable_paths: tuple[Path, ...] = field(default_factory=tuple)
    require_confirmation: bool = True

    @property
    def path_policy(self) -> PathAccessPolicy:
        return PathAccessPolicy(
            self.workspace,
            self.readable_paths,
            self.writable_paths,
        )


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    content: str
    # Optional image outputs as data URIs (e.g. "data:image/png;base64,...").
    # These are forwarded to the model as image_url content blocks.
    images: tuple[str, ...] = ()


class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    requires_confirmation: bool

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def descriptions(self) -> str:
        lines = []
        for tool in self._tools.values():
            lines.append(f"- {tool.name}: {tool.description}")
        return "\n".join(lines)

    def openai_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]


def resolve_workspace_path(
    workspace: Path,
    requested_path: str,
    *,
    access: str = "read",
    readable_paths: tuple[Path, ...] = (),
    writable_paths: tuple[Path, ...] = (),
) -> Path:
    return PathAccessPolicy(workspace, readable_paths, writable_paths).resolve(
        requested_path,
        access=access,
    )
