from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from uuid import uuid4
from typing import Any

try:
    from json_repair import repair_json as _repair_json
    _HAS_JSON_REPAIR = True
except ImportError:
    _HAS_JSON_REPAIR = False

from little_agent.messages import Message, text_content
from little_agent.tools.base import ToolRegistry


class LLMClient:
    def complete(
        self,
        model: str,
        messages: list[Message],
        tools: ToolRegistry,
    ) -> dict[str, Any]:
        raise NotImplementedError


class OpenAICompatibleChatClient(LLMClient):
    """Call an OpenAI-compatible Chat Completions API without the OpenAI SDK."""

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1", timeout: int = 60) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(
        self,
        model: str,
        messages: list[Message],
        tools: ToolRegistry,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [self._message_payload(message) for message in messages],
            "tools": tools.openai_schemas(),
            "tool_choice": "auto",
        }
        response = self._post_json("/chat/completions", body)
        choice = response["choices"][0]["message"]
        return {
            "content": choice.get("content") or "",
            "tool_calls": self._tool_calls(choice),
            "usage": response.get("usage") or {},
        }

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Chat completion API returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach chat completion API: {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"Chat completion API timed out after {self.timeout} seconds.") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Chat completion API returned invalid JSON: {raw[:500]}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("Chat completion API returned a non-object JSON response.")
        return parsed

    @staticmethod
    def _message_payload(message: Message) -> dict[str, Any]:
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.name:
            item["name"] = message.name
        if message.role == "tool" and message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            item["tool_calls"] = [_openai_tool_call(call) for call in message.tool_calls]
        return item

    @staticmethod
    def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            calls.append(
                {
                    "id": call.get("id") or f"call_{uuid4().hex[:12]}",
                    "name": function.get("name", ""),
                    "arguments": _json_object(function.get("arguments") or "{}"),
                }
            )

        legacy_call = message.get("function_call")
        if legacy_call:
            calls.append(
                {
                    "id": f"call_{uuid4().hex[:12]}",
                    "name": legacy_call.get("name", ""),
                    "arguments": _json_object(legacy_call.get("arguments") or "{}"),
                }
            )
        return [call for call in calls if call["name"]]


class LocalRuleClient(LLMClient):
    """A tiny no-API fallback so the CLI can be exercised immediately."""

    def complete(
        self,
        model: str,
        messages: list[Message],
        tools: ToolRegistry,
    ) -> dict[str, Any]:
        tool_messages = [message for message in messages if message.role == "tool"]
        if tool_messages:
            return {"content": text_content(tool_messages[-1].content), "tool_calls": [], "usage": {}}

        text = next(
            (text_content(message.content) for message in reversed(messages) if message.role == "user"),
            "",
        )
        lowered = text.lower()
        if "time" in lowered or "date" in lowered:
            return {"content": "", "tool_calls": [{"name": "get_datetime", "arguments": {}}], "usage": {}}
        if "list" in lowered or "ls" in lowered:
            return {"content": "", "tool_calls": [{"name": "list_dir", "arguments": {"path": "."}}], "usage": {}}
        return {
            "content": (
                "OPENAI_API_KEY is not configured, so I am running in local fallback mode. "
                "I can still handle simple requests like date/time or listing the workspace."
            ),
            "tool_calls": [],
            "usage": {},
        }


def _json_object(raw: str) -> dict[str, Any]:
    if _HAS_JSON_REPAIR:
        parsed = _repair_json(raw, return_objects=True)
        return parsed if isinstance(parsed, dict) else {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _openai_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    arguments = call.get("arguments") or {}
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": call.get("id") or f"call_{uuid4().hex[:12]}",
        "type": "function",
        "function": {
            "name": str(call.get("name") or ""),
            "arguments": arguments,
        },
    }
