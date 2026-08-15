"""Deterministic explicit-command parser; no LLM interpretation occurs here."""

from __future__ import annotations

import json
import shlex
import uuid

from .models import Command


class CommandParseError(ValueError):
    """Raised when an explicit slash command is malformed or unsupported."""


_COMMANDS = {
    "status": ("system.status", None),
    "goals": ("goals.list", None),
    "work": ("work.show", "work"),
    "pause": ("work.pause", "work"),
    "resume": ("work.resume", "work"),
    "cancel": ("work.cancel", "work"),
    "priority": ("work.priority", "work"),
    "deadline": ("work.deadline", "work"),
    "provider": ("work.provider", "work"),
    "approve": ("review.approve", "review"),
    "reject": ("review.reject", "review"),
    "context": ("work.context", "work"),
    "trace": ("work.trace", "work"),
}


def parse_explicit_command(
    text: str,
    *,
    issuer_identity_id: str,
    source_channel: str,
    source_message_id: str | None = None,
    idempotency_key: str | None = None,
) -> Command:
    """Parse one explicit slash command into the channel-neutral model."""

    try:
        tokens = shlex.split(text.strip())
    except ValueError as exc:
        raise CommandParseError(str(exc)) from exc
    if not tokens or not tokens[0].startswith("/"):
        raise CommandParseError("explicit commands must start with '/'")
    name = tokens.pop(0)[1:].lower()
    source_id = source_message_id or str(uuid.uuid4())
    key = idempotency_key or f"{source_channel}:{source_id}"
    if name == "task":
        return _create_command(
            "task.create", None, tokens, issuer_identity_id, source_channel, source_id, key,
            action_required="create",
        )
    if name == "goal":
        if not tokens:
            raise CommandParseError("/goal requires create, show, pause, resume, cancel, or evaluate")
        action = tokens.pop(0).lower()
        if action not in {"create", "show", "pause", "resume", "cancel", "evaluate"}:
            raise CommandParseError(f"unsupported /goal action {action!r}")
        target = None if action == "create" else "goal"
        return _create_command(
            f"goal.{action}", target, tokens, issuer_identity_id, source_channel, source_id, key
        )
    if name not in _COMMANDS:
        raise CommandParseError(f"unsupported explicit command '/{name}'")
    command_type, target_type = _COMMANDS[name]
    return _create_command(
        command_type, target_type, tokens, issuer_identity_id, source_channel, source_id, key
    )


def _create_command(
    command_type: str,
    target_type: str | None,
    tokens: list[str],
    issuer: str,
    channel: str,
    source_id: str,
    key: str,
    *,
    action_required: str | None = None,
) -> Command:
    if action_required:
        if not tokens or tokens.pop(0).lower() != action_required:
            raise CommandParseError(f"/{command_type.split('.')[0]} requires '{action_required}'")
    arguments: dict = {}
    positional: list[str] = []
    for token in tokens:
        if "=" in token:
            name, value = token.split("=", 1)
            if not name:
                raise CommandParseError("argument name must not be empty")
            arguments[name.replace("-", "_")] = _value(value)
        else:
            positional.append(token)
    target_id = positional.pop(0) if target_type and positional else None
    if positional:
        if command_type == "work.priority":
            arguments.setdefault("priority", positional.pop(0))
        elif command_type == "work.deadline":
            arguments.setdefault("deadline", positional.pop(0))
        elif command_type == "work.provider":
            arguments.setdefault("kind", positional.pop(0))
            if positional:
                arguments.setdefault("provider", positional.pop(0))
        else:
            arguments.setdefault("value", " ".join(positional))
    return Command(
        command_type=command_type,
        issuer_identity_id=issuer,
        source_channel=channel,
        source_message_id=source_id,
        target_type=target_type,
        target_id=target_id,
        arguments=arguments,
        idempotency_key=key,
    )


def _value(raw: str):
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if raw.startswith(("{", "[", '"')) or lowered in {"null"}:
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise CommandParseError(f"invalid JSON argument: {raw}") from exc
    return raw
