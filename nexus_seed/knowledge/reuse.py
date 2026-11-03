"""Explicit, condition-scoped reuse of human answers across Projects.

The ledger keeps the original question and answer unchanged. Reusability is a
human-approved Annotation, not a claim that the answer has been validated.
No fuzzy matching or implicit transfer between unrelated Projects is attempted.
"""

from __future__ import annotations

from typing import Any

from .ledger import KnowledgeLedger

REUSABLE_ANSWER_ANNOTATION = "reusable_answer"
MAX_REUSE_RESULTS = 16


def normalize_reuse_spec(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate an exact-match scope for a human-approved reusable answer."""
    if not isinstance(raw, dict):
        raise ValueError("reuse specification must be an object")
    subject = raw.get("subject")
    prop = raw.get("property")
    conditions = raw.get("conditions", {})
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("reuse.subject must be a non-empty string")
    if not isinstance(prop, str) or not prop.strip():
        raise ValueError("reuse.property must be a non-empty string")
    if len(subject) > 256 or len(prop) > 256:
        raise ValueError("reuse subject/property is too long")
    if not isinstance(conditions, dict):
        raise ValueError("reuse.conditions must be an object")
    if len(conditions) > 32:
        raise ValueError("too many reuse conditions")
    normalized: dict[str, str | int | float | bool] = {}
    for key, value in conditions.items():
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise ValueError("reuse condition keys must be short strings")
        if not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and (
            value != value or value in (float("inf"), float("-inf"))
        ):
            raise ValueError("reuse condition values must be finite scalars")
        normalized[key] = value
    return {
        "subject": subject.strip(),
        "property": prop.strip(),
        "conditions": normalized,
    }


class KnowledgeReuse:
    """Select explicitly shared answers that exactly match requested scope."""

    def __init__(self, ledger: KnowledgeLedger) -> None:
        self.ledger = ledger

    def for_project_context(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Return only unambiguous human-approved answers for explicit requests.

        Requests are passed as `knowledge_requirements` in a Project context.
        Missing/partial conditions never broaden the lookup. Two differing
        answers to the same exact scope are treated as a conflict and omitted.
        """
        requests = context.get("knowledge_requirements")
        if not isinstance(requests, list):
            return []

        answered = [
            item
            for item in self.ledger.by_kind("question")
            if item.status == "ANSWERED" and isinstance(item.metadata.get("answer"), str)
        ]
        found: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for raw in requests[:MAX_REUSE_RESULTS]:
            try:
                spec = normalize_reuse_spec(raw)
            except ValueError:
                continue
            signature = (
                spec["subject"],
                spec["property"],
                repr(sorted(spec["conditions"].items())),
            )
            if signature in seen:
                continue
            seen.add(signature)
            candidates = [
                item
                for item in answered
                if any(
                    ann.kind == REUSABLE_ANSWER_ANNOTATION and ann.value == spec
                    for ann in item.annotations
                )
            ]
            # A contradictory pair must not be arbitrarily resolved by date.
            answers = {item.metadata["answer"].strip() for item in candidates}
            answers.discard("")
            if len(answers) != 1:
                continue
            candidate = sorted(
                candidates, key=lambda item: (item.recorded_at, item.knowledge_id)
            )[-1]
            found.append(
                {
                    "knowledge_id": candidate.knowledge_id,
                    "subject": spec["subject"],
                    "property": spec["property"],
                    "conditions": dict(spec["conditions"]),
                    "answer": candidate.metadata["answer"],
                    "source": "human_answer",
                    "verification": "unverified",
                }
            )
        return found
