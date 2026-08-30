from __future__ import annotations

from little_agent.tools.base import ToolContext, ToolResult


class ListDirTool:
    name = "list_dir"
    description = "List files and directories under a workspace path."
    requires_confirmation = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative directory path."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        policy = context.path_policy
        path = policy.resolve(str(kwargs["path"]), access="read")
        if not path.exists():
            return ToolResult(False, f"Directory does not exist: {path}")
        if not path.is_dir():
            return ToolResult(False, f"Path is not a directory: {path}")
        rows = []
        for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            kind = "dir " if child.is_dir() else "file"
            rows.append(f"{kind} {policy.display(child)}")
        return ToolResult(True, "\n".join(rows) if rows else "(empty)")


class ReadFileTool:
    name = "read_file"
    description = "Read a UTF-8 text file inside the workspace."
    requires_confirmation = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        path = context.path_policy.resolve(str(kwargs["path"]), access="read")
        if not path.exists():
            return ToolResult(False, f"File does not exist: {path}")
        if not path.is_file():
            return ToolResult(False, f"Path is not a file: {path}")
        return ToolResult(True, path.read_text(encoding="utf-8"))


class WriteFileTool:
    name = "write_file"
    description = "Write a UTF-8 text file inside the workspace. Creates parent directories."
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "content": {"type": "string", "description": "Complete file content."},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        policy = context.path_policy
        path = policy.resolve(str(kwargs["path"]), access="write")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(kwargs["content"]), encoding="utf-8")
        return ToolResult(True, f"Wrote {policy.display(path)}")


class AppendFileTool:
    name = "append_to_file"
    description = "Append UTF-8 text to a file inside the workspace. Creates the file if missing."
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "content": {"type": "string", "description": "Text to append to the end of the file."},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        policy = context.path_policy
        path = policy.resolve(str(kwargs["path"]), access="write")
        if path.exists() and not path.is_file():
            return ToolResult(False, f"Path is not a file: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(str(kwargs["content"]))
        return ToolResult(True, f"Appended to {policy.display(path)}")


class DeleteFileTool:
    name = "delete_file"
    description = "Delete a file inside the workspace."
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        policy = context.path_policy
        path = policy.resolve(str(kwargs["path"]), access="write")
        if not path.exists():
            return ToolResult(False, f"File does not exist: {path}")
        if not path.is_file():
            return ToolResult(False, f"Path is not a file: {path}")
        path.unlink()
        return ToolResult(True, f"Deleted {policy.display(path)}")


class MoveFileTool:
    name = "move_file"
    description = "Move or rename a file inside the workspace. Creates destination parent directories."
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Workspace-relative source file path."},
            "destination": {"type": "string", "description": "Workspace-relative destination file path."},
        },
        "required": ["source", "destination"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        policy = context.path_policy
        source = policy.resolve(str(kwargs["source"]), access="write")
        destination = policy.resolve(str(kwargs["destination"]), access="write")
        if not source.exists():
            return ToolResult(False, f"Source does not exist: {source}")
        if not source.is_file():
            return ToolResult(False, f"Source is not a file: {source}")
        if destination.exists():
            return ToolResult(False, f"Destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
        rel_source = policy.display(source)
        rel_destination = policy.display(destination)
        return ToolResult(True, f"Moved {rel_source} -> {rel_destination}")


class SearchFilesTool:
    name = "search_files"
    description = "Search for text in UTF-8 files under a workspace path."
    requires_confirmation = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative directory path."},
            "query": {"type": "string", "description": "Text to search for."},
        },
        "required": ["path", "query"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        policy = context.path_policy
        root = policy.resolve(str(kwargs["path"]), access="read")
        query = str(kwargs["query"])
        if not root.exists():
            return ToolResult(False, f"Path does not exist: {root}")
        files = [root] if root.is_file() else [item for item in root.rglob("*") if item.is_file()]
        matches: list[str] = []
        for file_path in files:
            try:
                file_path = policy.resolve(str(file_path), access="read")
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, ValueError):
                continue
            for index, line in enumerate(lines, start=1):
                if query in line:
                    rel = policy.display(file_path)
                    matches.append(f"{rel}:{index}: {line}")
        return ToolResult(True, "\n".join(matches[:200]) if matches else "(no matches)")
