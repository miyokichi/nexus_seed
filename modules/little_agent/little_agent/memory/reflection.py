"""Distil a finished session into durable profiles.

The reflector is the *learning* half of :mod:`little_agent.memory`: it reads a
session transcript plus the profiles that already exist and asks the model for
merged replacements. Only a store that actually persists something drives it
(:class:`~little_agent.memory.store.FileMemoryStore`); the null store never does,
so a ``--no-memory`` chat and every A2A task learn nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from little_agent.llm import LLMClient
from little_agent.messages import Message, text_content
from little_agent.tools.base import ToolRegistry

try:
    from json_repair import repair_json as _repair_json
    _HAS_JSON_REPAIR = True
except ImportError:
    _HAS_JSON_REPAIR = False

# Keep each learned profile bounded so it never bloats the system prompt.
PROFILE_MAX_CHARS = 2000
# Cap how much of a single turn we feed into the reflection prompt.
_TURN_MAX_CHARS = 1200

_SYSTEM_PROMPT = (
    "You maintain a durable memory of the user across sessions for an AI agent called Little Agent. "
    "You are given the CURRENT profiles and the RECENT session transcript. "
    "Produce UPDATED profiles by merging any durable, reusable knowledge from the session.\n\n"
    "GLOBAL profile: the user's lasting preferences, working style, language, tone, recurring "
    "intents, and how they like the agent to behave. Applies to every project.\n"
    "WORKSPACE profile: essence specific to THIS project/workspace - what it is, key decisions, "
    "conventions, and recurring tasks here.\n\n"
    "Rules:\n"
    "- Merge, do not just append. Deduplicate and rewrite for clarity.\n"
    "- Keep only durable, reusable facts. Drop one-off details and transient task state.\n"
    f"- Keep each profile concise (under ~{PROFILE_MAX_CHARS} characters), as short markdown bullet lists.\n"
    "- Preserve still-relevant existing entries; refine them with new evidence.\n"
    "- Write the profiles in the user's primary language.\n"
    "- If nothing durable was learned, return the existing profiles unchanged.\n\n"
    'Respond with ONLY a JSON object: {"global_profile": "<markdown>", "workspace_profile": "<markdown>"}.'
)


@dataclass(slots=True)
class Reflector:
    """Distills a session transcript into durable user/workspace profiles via the LLM."""

    llm: LLMClient
    model: str

    def reflect(
        self,
        messages: list[Message],
        global_profile: str,
        workspace_profile: str,
    ) -> tuple[str, str] | None:
        """Return merged (global_profile, workspace_profile), or None on failure/no-op.

        An empty string for a profile means "leave the existing one untouched" so the
        caller never wipes a profile because the model omitted it.
        """
        transcript = self._transcript(messages)
        if not transcript.strip():
            return None

        payload = (
            f"# Current global profile\n{global_profile or '(empty)'}\n\n"
            f"# Current workspace profile\n{workspace_profile or '(empty)'}\n\n"
            f"# Recent session transcript\n{transcript}"
        )
        prompt = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=payload),
        ]
        try:
            response = self.llm.complete(self.model, prompt, ToolRegistry())
        except Exception:  # noqa: BLE001 - reflection must never break the session.
            return None

        parsed = _parse_json_object(text_content(response.get("content", "")))
        if parsed is None:
            return None
        new_global = _clean_profile(parsed.get("global_profile"))
        new_workspace = _clean_profile(parsed.get("workspace_profile"))
        return new_global, new_workspace

    @staticmethod
    def _transcript(messages: list[Message]) -> str:
        lines: list[str] = []
        for message in messages:
            if message.role not in ("user", "assistant"):
                continue
            text = text_content(message.content).strip()
            if not text:
                continue
            if len(text) > _TURN_MAX_CHARS:
                text = text[:_TURN_MAX_CHARS] + "…"
            speaker = "User" if message.role == "user" else "Assistant"
            lines.append(f"{speaker}: {text}")
        return "\n".join(lines)


def _parse_json_object(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    if _HAS_JSON_REPAIR:
        parsed = _repair_json(raw, return_objects=True)
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _clean_profile(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    if len(text) > PROFILE_MAX_CHARS:
        text = text[:PROFILE_MAX_CHARS].rstrip() + "…"
    return text
