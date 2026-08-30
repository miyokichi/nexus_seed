"""A2A protocol data shapes and JSON-RPC helpers.

Wire objects are plain dicts matching the A2A specification, built through the
helpers here so the shapes stay in one place. Only the subset Little Agent
implements is modelled: Agent Card, Message, Part, Task, TaskStatus, Artifact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

PROTOCOL_VERSION = "0.3.0"

# Canonical Agent Card path (the pre-0.3 path is also served for compatibility).
AGENT_CARD_PATH = "/.well-known/agent-card.json"
LEGACY_AGENT_CARD_PATH = "/.well-known/agent.json"

# TaskState values from the spec.
TASK_SUBMITTED = "submitted"
TASK_WORKING = "working"
TASK_INPUT_REQUIRED = "input-required"
TASK_COMPLETED = "completed"
TASK_CANCELED = "canceled"
TASK_FAILED = "failed"
TASK_REJECTED = "rejected"

TERMINAL_STATES = frozenset({TASK_COMPLETED, TASK_CANCELED, TASK_FAILED, TASK_REJECTED})

# JSON-RPC standard errors.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# A2A-specific errors.
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOTIFICATION_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005

# Metadata key carrying delegation depth between Little Agent peers, so a chain
# of A2A hand-offs cannot recurse forever. Namespaced per the spec's guidance.
DEPTH_METADATA_KEY = "littleAgent/delegationDepth"


class A2AError(Exception):
    """A JSON-RPC error to return to the caller."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_json(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id() -> str:
    return uuid4().hex


def text_part(text: str) -> dict[str, Any]:
    return {"kind": "text", "text": text}


def data_part(data: Any) -> dict[str, Any]:
    return {"kind": "data", "data": data}


def parts_to_text(parts: Any) -> str:
    """Concatenate the text parts of a Message/Artifact, ignoring other kinds."""

    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "text":
            chunks.append(str(part.get("text") or ""))
    return "\n".join(chunk for chunk in chunks if chunk)


def parts_to_data(parts: Any) -> list[Any]:
    """The payloads of every DataPart in a Message/Artifact, in order."""

    if not isinstance(parts, list):
        return []
    return [
        part.get("data")
        for part in parts
        if isinstance(part, dict) and part.get("kind") == "data" and "data" in part
    ]


# Keys a DataPart may use to address the runtime directly. Any other DataPart
# content is treated as context for the task.
INSTRUCTION_KEY = "instruction"
CONTEXT_KEY = "context"
OUTPUT_SCHEMA_KEY = "output_schema"
REQUEST_KEYS = frozenset({INSTRUCTION_KEY, CONTEXT_KEY, OUTPUT_SCHEMA_KEY})


@dataclass(slots=True)
class RequestPayload:
    """What an incoming A2A Message asks the agent to do.

    Text parts carry the instruction; DataParts carry structured input. A
    DataPart may name the request fields explicitly::

        {"instruction": "...", "context": {...}, "output_schema": {...}}

    and any DataPart without those keys is merged into ``context`` as-is, so a
    caller can simply send its payload.
    """

    instruction: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None


def parse_request_parts(parts: Any) -> RequestPayload:
    """Read an incoming Message's parts into a RequestPayload."""

    payload = RequestPayload()
    text = parts_to_text(parts).strip()
    instructions = [text] if text else []

    for data in parts_to_data(parts):
        if not isinstance(data, dict):
            # A bare list/scalar DataPart is still input; keep it addressable.
            bare = payload.context.get("data")
            payload.context["data"] = [*bare, data] if isinstance(bare, list) else [data]
            continue
        if not REQUEST_KEYS & data.keys():
            payload.context.update(data)
            continue
        instruction = data.get(INSTRUCTION_KEY)
        if isinstance(instruction, str) and instruction.strip():
            instructions.append(instruction.strip())
        context = data.get(CONTEXT_KEY)
        if isinstance(context, dict):
            payload.context.update(context)
        elif context is not None:
            payload.context[CONTEXT_KEY] = context
        schema = data.get(OUTPUT_SCHEMA_KEY)
        if isinstance(schema, dict):
            payload.output_schema = schema

    payload.instruction = "\n\n".join(instructions)
    return payload


def new_message(
    role: str,
    text: str = "",
    *,
    data: Any = None,
    task_id: str | None = None,
    context_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if text:
        parts.append(text_part(text))
    if data is not None:
        parts.append(data_part(data))
    message: dict[str, Any] = {
        "kind": "message",
        "role": role,
        "parts": parts,
        "messageId": new_id(),
    }
    if task_id:
        message["taskId"] = task_id
    if context_id:
        message["contextId"] = context_id
    if metadata:
        message["metadata"] = metadata
    return message


def new_task(task_id: str, context_id: str, state: str = TASK_SUBMITTED) -> dict[str, Any]:
    return {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {"state": state, "timestamp": now_iso()},
        "artifacts": [],
        "history": [],
    }


def text_artifact(text: str, name: str = "result") -> dict[str, Any]:
    return {
        "artifactId": new_id(),
        "name": name,
        "parts": [text_part(text)],
    }


def result_artifact(text: str, data: Any = None, name: str = "result") -> dict[str, Any]:
    """The finished task's deliverable: a DataPart when structured, else text."""

    if data is None:
        return text_artifact(text, name)
    return {"artifactId": new_id(), "name": name, "parts": [data_part(data)]}


def _parts_output(parts: Any) -> str:
    """Readable text for a set of parts, rendering DataParts as JSON."""

    chunks: list[str] = []
    text = parts_to_text(parts)
    if text:
        chunks.append(text)
    for data in parts_to_data(parts):
        chunks.append(json.dumps(data, ensure_ascii=False))
    return "\n\n".join(chunks)


def task_result_text(task: dict[str, Any]) -> str:
    """Best-effort extraction of a finished task's output as text.

    Prefers artifacts (where an agent puts its deliverable) and falls back to the
    final status message, which is where a failure reason usually lands.
    """

    chunks: list[str] = []
    for artifact in task.get("artifacts") or []:
        if isinstance(artifact, dict):
            chunk = _parts_output(artifact.get("parts"))
            if chunk:
                chunks.append(chunk)
    if chunks:
        return "\n\n".join(chunks)
    status = task.get("status")
    if isinstance(status, dict):
        message = status.get("message")
        if isinstance(message, dict):
            return _parts_output(message.get("parts"))
    return ""


def task_result_data(task: dict[str, Any]) -> Any | None:
    """The first DataPart payload in a finished task's artifacts, if any."""

    for artifact in task.get("artifacts") or []:
        if isinstance(artifact, dict):
            for data in parts_to_data(artifact.get("parts")):
                return data
    return None


def agent_card(
    *,
    name: str,
    description: str,
    url: str,
    version: str,
    skills: list[dict[str, Any]],
    requires_auth: bool = False,
) -> dict[str, Any]:
    """Build the Agent Card advertised at the well-known endpoint."""

    card: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "name": name,
        "description": description,
        "url": url,
        "preferredTransport": "JSONRPC",
        "version": version,
        "capabilities": {
            # This implementation is blocking + polling: no SSE, no push.
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        # Both plain text and structured JSON (A2A TextPart / DataPart) are accepted
        # as input and can be returned as the result.
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": skills,
    }
    if requires_auth:
        card["securitySchemes"] = {
            "bearer": {"type": "http", "scheme": "bearer", "description": "Shared bearer token."}
        }
        card["security"] = [{"bearer": []}]
    return card


def agent_skill(
    skill_id: str,
    name: str,
    description: str,
    tags: list[str] | None = None,
    examples: list[str] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": skill_id,
        "name": name,
        "description": description,
        "tags": tags or [],
    }
    if examples:
        entry["examples"] = examples
    return entry


def rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: Any, error: A2AError) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": error.to_json()}
