from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def estimate_tokens(text: str) -> int:
    # Cheap fallback for APIs that do not return usage. Good enough for trend tracking.
    return max(1, (len(text) + 3) // 4) if text else 0


def truncate(value: object, limit: int = 4000) -> object:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "...[truncated]"
    if isinstance(value, dict):
        return {key: truncate(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [truncate(item, limit) for item in value]
    return value


@dataclass(slots=True)
class UsageTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_tokens: int = 0

    def add(self, usage: dict[str, object]) -> None:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or prompt + completion)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total
        if usage.get("estimated"):
            self.estimated_tokens += total


@dataclass(slots=True)
class RunLogger:
    log_dir: Path
    session_id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    usage_totals: UsageTotals = field(default_factory=UsageTotals)

    def __post_init__(self) -> None:
        for child in ["conversations", "tools", "usage"]:
            (self.log_dir / child).mkdir(parents=True, exist_ok=True)

    def log_conversation(self, event: str, **fields: object) -> None:
        self._write("conversations", event, fields)

    def log_tool(self, event: str, **fields: object) -> None:
        self._write("tools", event, fields)

    def log_usage(self, event: str, usage: dict[str, object], **fields: object) -> None:
        self.usage_totals.add(usage)
        fields["usage"] = usage
        fields["totals"] = {
            "prompt_tokens": self.usage_totals.prompt_tokens,
            "completion_tokens": self.usage_totals.completion_tokens,
            "total_tokens": self.usage_totals.total_tokens,
            "estimated_tokens": self.usage_totals.estimated_tokens,
        }
        self._write("usage", event, fields)

    def _write(self, category: str, event: str, fields: dict[str, object]) -> None:
        record = {
            "timestamp": now_iso(),
            "session_id": self.session_id,
            "event": event,
            **truncate(fields),
        }
        path = self.log_dir / category / f"{self.session_id}.jsonl"
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
