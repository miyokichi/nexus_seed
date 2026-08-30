from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


Role = Literal["system", "user", "assistant", "tool"]

# Content is either plain text, or a list of OpenAI-style content blocks
# (e.g. {"type": "text", ...} and {"type": "image_url", ...}) for multimodal input.
Content = str | list[dict[str, Any]]


@dataclass(slots=True)
class Message:
    role: Role
    content: Content
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


def text_content(content: Content) -> str:
    """Extract plain text from a message content (string or content-block list)."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)
