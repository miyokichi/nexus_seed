"""Deterministic guards applied before a question ever reaches the LLM.

Two things must not depend on a model behaving well:

* a request to *change* something is refused, because this phase is read-only;
* a question about another project is refused, because a thread is scoped to
  exactly one ``project_id``.

Both are decided here by explicit patterns over the message text.  The guards
are a boundary, not the guarantee: Project Chat has no write path at all, so a
missed pattern still cannot change state — it only reaches an LLM that is also
instructed to refuse.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: "<noun>して" requests: キャンセルして, 優先して, 承認してください, …
_JAPANESE_SURU_CHANGE = re.compile(
    r"(キャンセル|中止|取り消し?|取消|停止|一時停止|再開|優先|承認|却下|実行|開始"
    r"|着手|変更|修正|削除|追加|割り当て|アサイン|再計画|リプラン|やり直し?)"
    # Longest first: Python alternation stops at the first match, and the
    # reported phrase should be the whole request, not its prefix.
    r"\s*(してください|して下さい|してほしい|して欲しい|しといて|してくれ"
    r"|をお願い|お願い|して|しろ|せよ)"
)

#: Verb requests that need no する: 進めて, 止めて, やめてください, …
_JAPANESE_VERB_CHANGE = re.compile(
    r"(進め|止め|やめ|外し|戻し|直し|変え)"
    r"\s*(てください|て下さい|てほしい|て欲しい|ておいて|とい[てで]|てくれ|て|ろ|よ)"
)

#: A verb in the imperative or in a "can you …" request, in English.
_ENGLISH_CHANGE = re.compile(
    r"(?:^|[.!?]\s+|\b(?:please|can|could|would|will|let's|lets)\s+(?:you\s+)?(?:please\s+)?)"
    r"(cancel|stop|pause|resume|start|begin|approve|reject|prioriti[sz]e|replan|"
    r"execute|run|retry|assign|delete|remove|add|change|update|modify|escalate)\b",
    re.IGNORECASE,
)


def detect_state_change_request(message: str) -> str | None:
    """Return the phrase asking for a state change, or ``None`` for a question."""

    text = (message or "").strip()
    if not text:
        return None
    for pattern in (_JAPANESE_SURU_CHANGE, _JAPANESE_VERB_CHANGE, _ENGLISH_CHANGE):
        match = pattern.search(text)
        if match is not None:
            return match.group(0).strip()
    return None


def detect_foreign_projects(
    message: str, *, project_id: str, known_projects: Iterable[Any]
) -> list[str]:
    """Return identifiers of other projects the message explicitly names.

    Only projects that already exist are considered.  A thread never searches
    another project to answer, so naming one is reported back as out of scope
    rather than resolved (Invariant 191).
    """

    text = (message or "").lower()
    if not text:
        return []
    current = {value.lower() for value in _identities(project_id, project_id) if value}
    found: list[str] = []
    for project in known_projects:
        other_id = str(getattr(project, "project_id", "") or "")
        if not other_id or other_id == project_id:
            continue
        title = str(getattr(project, "title", "") or "")
        for candidate in _identities(other_id, title):
            lowered = candidate.lower()
            if lowered in current or len(lowered) < 3:
                continue
            if _mentions(text, lowered) and other_id not in found:
                found.append(other_id)
    return found


def _identities(project_id: str, title: str) -> tuple[str, ...]:
    """The names one project can reasonably be called by in a message."""

    values = {value.strip() for value in (project_id, title) if value and value.strip()}
    return tuple(sorted(values))


def _mentions(text: str, candidate: str) -> bool:
    """Whether ``candidate`` appears as a word rather than inside a longer name."""

    pattern = re.compile(
        rf"(?<![0-9a-z_\-]){re.escape(candidate)}(?![0-9a-z_\-])", re.IGNORECASE
    )
    return pattern.search(text) is not None


__all__ = ["detect_foreign_projects", "detect_state_change_request"]
