"""Code-generation boundary: model output becomes validated sandbox artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..backends.base import BackendRequest, LLMInvocation
from ..extension.models import ExtensionStrategy
from .models import ArtifactRole
from .validator import validate_relative_path


ARTIFACT_SCHEMA = {
    "type": "object",
    "required": ["artifacts"],
    "properties": {
        "artifacts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["relative_path", "content", "artifact_role"],
            },
        }
    },
}


@dataclass(frozen=True)
class GeneratedArtifact:
    relative_path: str
    content: str
    artifact_role: str


def validate_generated_artifacts(
    value: Any, *, max_files: int, max_file_bytes: int, max_total_bytes: int
) -> tuple[list[GeneratedArtifact], list[str]]:
    reasons: list[str] = []
    raw = value.get("artifacts") if isinstance(value, dict) else None
    if not isinstance(raw, list):
        return [], ["generator output must contain an artifacts array"]
    if len(raw) > max_files:
        reasons.append(f"artifact count {len(raw)} exceeds limit {max_files}")
    roles = {r.value for r in ArtifactRole}
    artifacts: list[GeneratedArtifact] = []
    total = 0
    paths: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            reasons.append(f"artifact {index} is not an object")
            continue
        path = str(item.get("relative_path", ""))
        content = item.get("content")
        role = str(item.get("artifact_role", ""))
        path_reason = validate_relative_path(path)
        if path_reason:
            reasons.append(path_reason)
        if path in paths:
            reasons.append(f"duplicate artifact path {path!r}")
        paths.add(path)
        if not isinstance(content, str):
            reasons.append(f"artifact {path!r} content must be text")
            continue
        size = len(content.encode("utf-8"))
        total += size
        if size > max_file_bytes:
            reasons.append(f"artifact {path!r} exceeds per-file size limit")
        if role not in roles:
            reasons.append(f"unknown artifact role {role!r}")
        artifacts.append(GeneratedArtifact(path, content, role))
    if total > max_total_bytes:
        reasons.append(f"artifact total size {total} exceeds limit {max_total_bytes}")
    return artifacts, list(dict.fromkeys(reasons))


def deterministic_artifacts(plan, proposal) -> list[GeneratedArtifact]:
    """A testable floor used when no model is configured."""
    target = plan.target_capabilities[0] if plan.target_capabilities else "capability"
    result = {"type": "dict", "capability": target, "verified": True}
    artifacts: list[GeneratedArtifact] = []
    for expected in plan.expected_artifacts:
        path = expected.relative_path
        role = expected.artifact_role
        if path.endswith(".py") and role == ArtifactRole.IMPLEMENTATION.value:
            content = (
                '"""Sandbox-generated capability candidate; not installed."""\n\n'
                "def nexus_capability(fixture):\n"
                f"    return {result!r}\n"
            )
        elif path.endswith(".py"):
            content = (
                '"""Sandbox-only verification artifact."""\n\n'
                "def test_candidate_shape():\n"
                "    assert True\n"
            )
        elif path.endswith(".json"):
            import json
            content = json.dumps({
                "strategy": proposal.strategy.value if proposal.strategy else None,
                "target_capabilities": plan.target_capabilities,
                "production_activation": False,
            }, sort_keys=True)
        else:
            content = f"fixture for {target}\n"
        artifacts.append(GeneratedArtifact(path, content, role))
    return artifacts


class LLMConstructionGenerator:
    """Ask an ExecutionBackend for artifact text; never write or execute it."""

    def __init__(self, backend, *, backend_name: str = "llm") -> None:
        self.backend = backend
        self.backend_name = backend_name

    async def generate(self, plan, proposal, *, process_instance_id, context=None):
        request = BackendRequest(
            instruction=(
                "Generate only the declared sandbox artifacts. Return JSON with an "
                "artifacts array; do not return shell commands, install steps, or "
                "production paths. Each implementation must expose "
                "nexus_capability(fixture)."
            ),
            context={
                "proposal": {"id": str(proposal.id), "strategy": proposal.declared_strategy},
                "target_capabilities": plan.target_capabilities,
                "expected_artifacts": [a.to_dict() for a in plan.expected_artifacts],
                "compiled_context": context or {},
            },
            output_schema=ARTIFACT_SCHEMA,
            metadata={"construction_plan_id": str(plan.id)},
        )
        result = await self.backend.execute(request)
        invocation = LLMInvocation(
            process_instance_id=process_instance_id,
            backend=self.backend_name,
            model=result.model,
            request_metadata=request.metadata,
            response_metadata={"usage": result.usage or {}},
            success=result.success,
            error=result.error,
        )
        return result.parsed_output if result.parsed_output is not None else result.raw_output, invocation


__all__ = [
    "ARTIFACT_SCHEMA", "GeneratedArtifact", "LLMConstructionGenerator",
    "deterministic_artifacts", "validate_generated_artifacts",
]
