"""The canonical context a Project Chat answer may be compiled from.

The LLM never queries SQLite, Runtime state or another project.  It receives
one compacted Project Situation projection, the recent turns of this project's
own thread, and the current question — nothing else (Invariant 189).  Raw event
payloads are deliberately left out: they are audit detail, not the material an
explanation needs, and they are the easiest way to leak unrelated records into
a project-scoped answer.
"""

from __future__ import annotations

from typing import Any

from ..projects.models import ProjectSituation

#: How many records of each kind the answer context carries.
LIST_LIMIT = 12
#: How many recent changes are enough to answer "what changed lately?".
CHANGE_LIMIT = 10


def build_chat_context(
    situation: ProjectSituation,
    history: list,
    message: str,
) -> dict[str, Any]:
    """Compile the complete, project-scoped context for one question."""

    return {
        "project_situation": compact_situation(situation),
        "recent_chat": [
            {
                "role": item.role.value,
                "text": item.text,
                "status": item.status.value,
                "created_at": item.created_at.isoformat(),
            }
            for item in history
        ],
        "question": message,
    }


def compact_situation(situation: ProjectSituation) -> dict[str, Any]:
    """Return the projection with audit-only detail removed."""

    return {
        "project_id": situation.project_id,
        "title": situation.title,
        "objective": situation.objective,
        "overall_status": situation.overall_status.value,
        "summary": situation.summary,
        "updated_at": situation.updated_at.isoformat() if situation.updated_at else None,
        # The root Goal is what the project *is*, so it is stated once at the
        # top rather than left to be inferred from the goal list below.
        "root_goal": (
            _pick(situation.goal, ("id", "title", "objective", "status", "priority", "deadline"))
            if situation.goal
            else None
        ),
        "current_intention": (
            _pick(situation.current_intention, ("id", "focus", "status", "reason"))
            if situation.current_intention
            else None
        ),
        "active_goals": [
            _pick(item, ("id", "title", "objective", "status", "priority", "deadline"))
            for item in situation.active_goals[:LIST_LIMIT]
        ],
        "current_intentions": [
            _pick(item, ("id", "goal_id", "focus", "status", "reason"))
            for item in situation.current_intentions[:LIST_LIMIT]
        ],
        "active_work": [_work(item) for item in situation.active_work[:LIST_LIMIT]],
        "blocked_work": [_work(item) for item in situation.blocked_work[:LIST_LIMIT]],
        "recently_completed_work": [
            _work(item) for item in situation.recently_completed_work[:LIST_LIMIT]
        ],
        "blockers": [
            _pick(
                item,
                (
                    "type",
                    "severity",
                    "summary",
                    "work_id",
                    "goal_id",
                    "intention_id",
                    "review_id",
                    "dependency",
                    "missing_capabilities",
                ),
            )
            for item in situation.blockers[:LIST_LIMIT]
        ],
        "pending_reviews": [
            _pick(item, ("id", "event_type", "process", "created_at"))
            for item in situation.pending_reviews[:LIST_LIMIT]
        ],
        "unresolved_questions": [
            _pick(item, ("id", "text", "updated_at"))
            for item in situation.unresolved_questions[:LIST_LIMIT]
        ],
        "recent_changes": [
            _pick(item, ("type", "id", "summary", "status", "occurred_at"))
            for item in situation.recent_changes[:CHANGE_LIMIT]
        ],
        "recent_event_types": [
            item.get("type") for item in situation.recent_events[:CHANGE_LIMIT]
        ],
    }


def known_references(situation: ProjectSituation) -> set[str]:
    """Every identifier an answer may legitimately point at."""

    groups = (
        situation.active_goals,
        situation.current_intentions,
        situation.active_work,
        situation.blocked_work,
        situation.recently_completed_work,
        situation.pending_reviews,
        situation.unresolved_questions,
        situation.recent_events,
        situation.recent_changes,
        situation.blockers,
    )
    identifiers = {
        str(value)
        for group in groups
        for item in group
        for key, value in item.items()
        if key == "id" or key.endswith("_id")
        if value
    }
    identifiers.add(situation.project_id)
    return identifiers


def deterministic_answer(situation: ProjectSituation) -> str:
    """Describe the situation without an LLM, using projection facts only.

    Used when no LLM is configured or its answer cannot be trusted.  It is
    plainly labelled in the response status, so the interface never presents a
    fallback as if the model had answered.
    """

    lines = [situation.summary or f"{situation.title} の状況を読み込みました。"]
    if situation.goal:
        lines.append(
            f"Goal: {situation.goal['title']}（{situation.goal['status']}）"
        )
    elif situation.active_goals:
        titles = "、".join(item["title"] for item in situation.active_goals[:3])
        lines.append(f"進行中のGoal: {titles}")
    if situation.current_intention:
        lines.append(f"現在のIntention: {situation.current_intention['focus']}")
    elif situation.current_intentions:
        focuses = "、".join(item["focus"] for item in situation.current_intentions[:3])
        lines.append(f"現在のIntention: {focuses}")
    if situation.remaining_tasks:
        lines.append(f"残りのWork: {_work_lines(situation.remaining_tasks)}")
    if situation.blocked_tasks:
        lines.append(f"停止中のWork: {_work_lines(situation.blocked_tasks)}")
    if situation.completed_total:
        lines.append(f"完了したWork: {situation.completed_total}件")
    if situation.blockers:
        summaries = "、".join(
            str(item.get("summary") or item.get("type")) for item in situation.blockers[:3]
        )
        lines.append(f"Blocker: {summaries}")
    if situation.pending_reviews:
        lines.append(f"人間のReview待ち: {len(situation.pending_reviews)}件")
    else:
        lines.append("人間のReview待ちはありません。")
    if situation.recent_changes:
        lines.append(f"直近の変化: {situation.recent_changes[0]['summary']}")
    return "\n".join(lines)


def _work_lines(items) -> str:
    return "、".join(
        f"{item['objective'] or item['type']}（{item['status']}）" for item in items[:3]
    )


def _work(item: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        item,
        (
            "id",
            "goal_id",
            "type",
            "objective",
            "status",
            "reason",
            "priority",
            "missing_capabilities",
            "updated_at",
        ),
    )


def _pick(item: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: item[key] for key in keys if key in item and item[key] not in (None, [])}


__all__ = [
    "CHANGE_LIMIT",
    "LIST_LIMIT",
    "build_chat_context",
    "compact_situation",
    "deterministic_answer",
    "known_references",
]
