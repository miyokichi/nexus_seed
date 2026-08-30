"""The single default Project Executor used by MVP."""

from __future__ import annotations

import json
from pathlib import Path
from ..platform.contracts.interfaces import LLMProvider
from ..platform.contracts.mvp import (
    JsonObject,
    JsonValue,
    ProjectExecutionRequest,
    ProjectResult,
    ProjectResultStatus,
)

_RESULT_SCHEMA = {
    "type": "object",
    "required": ["summary", "outputs"],
    "properties": {
        "summary": {"type": "string"},
        "outputs": {"type": "object"},
    },
}


class DefaultProjectExecutor:
    """Execute one Project with an LLM and a minimal read-only workspace tool.

    The only MVP tool reads ``README.md`` when the goal asks about a README and
    a workspace was explicitly supplied.  It cannot write or execute commands.
    """

    def __init__(self, llm: LLMProvider, *, max_read_bytes: int = 100_000) -> None:
        if max_read_bytes <= 0:
            raise ValueError("max_read_bytes must be greater than zero")
        self.llm = llm
        self.max_read_bytes = max_read_bytes

    def execute(self, request: ProjectExecutionRequest) -> ProjectResult:
        """Return a completed result, or a failed result with the error recorded."""

        try:
            workspace_context = self._workspace_context(request)
            response = self.llm.generate(
                [
                    {
                        "role": "system",
                        "content": (
                            "You execute one approved project. Return a concise summary "
                            "and concrete outputs. Do not claim to have changed files."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "goal": request.goal,
                                "context": request.context,
                                "constraints": request.constraints,
                                "workspace_evidence": workspace_context,
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                    },
                ],
                schema=_RESULT_SCHEMA,
            )
            summary, outputs = _parse_response(response)
            return ProjectResult(
                project_id=request.project_id,
                status=ProjectResultStatus.COMPLETED,
                summary=summary,
                outputs=outputs,
            )
        except Exception as exc:  # noqa: BLE001 - executor turns its boundary failure into a result
            return ProjectResult(
                project_id=request.project_id,
                status=ProjectResultStatus.FAILED,
                summary="Project execution failed",
                error=str(exc),
            )

    def _workspace_context(self, request: ProjectExecutionRequest) -> JsonObject:
        if not request.workspace or "readme" not in request.goal.casefold():
            return {}
        root = Path(request.workspace).expanduser().resolve()
        readme = (root / "README.md").resolve()
        if readme.parent != root or not readme.is_file():
            return {}
        with readme.open("rb") as stream:
            raw = stream.read(self.max_read_bytes + 1)
        truncated = len(raw) > self.max_read_bytes
        raw = raw[: self.max_read_bytes]
        return {
            "path": "README.md",
            "content": raw.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }


def _parse_response(response: JsonValue) -> tuple[str, JsonObject]:
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            return response.strip(), {}
    if not isinstance(response, dict):
        raise ValueError("LLM response must be an object or text")
    summary = response.get("summary")
    outputs = response.get("outputs", {})
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("LLM response needs a non-empty summary")
    if not isinstance(outputs, dict):
        raise ValueError("LLM response outputs must be an object")
    return summary.strip(), dict(outputs)


__all__ = ["DefaultProjectExecutor"]
