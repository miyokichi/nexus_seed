"""Read-only Cockpit projection assembled from existing durable stores.

Nothing in this module writes SQLite or changes Runtime state.  It translates
the existing Event/Process/State/Goal/Intention/Review journals into a compact
human-facing view; raw facts remain attached for drill-down and audit.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from typing import Any

from ..actions.models import ActionExecutionStatus
from ..core.process import ProcessStatus
from ..presence.models import ClaimStatus, IntentionStatus
from ..presence.projections import get_intentions, project_master, project_self
from ..work.work_requirement import WorkStatus


ACTIVE_WORK = {
    WorkStatus.EXPECTED,
    WorkStatus.MATCHED,
    WorkStatus.SPAWNED,
    WorkStatus.PLANNED,
    WorkStatus.WAITING_REVIEW,
    WorkStatus.PAUSED,
}
BLOCKED_WORK = {
    WorkStatus.BLOCKED_CAPABILITY,
    WorkStatus.BLOCKED_PROVIDER,
    WorkStatus.BLOCKED_PLAN,
}
RUNNING_PROCESSES = {
    ProcessStatus.RUNNABLE,
    ProcessStatus.RUNNING,
    ProcessStatus.RETRY_WAIT,
    ProcessStatus.SUSPENDED,
}


class CockpitService:
    """Compile a current Cockpit snapshot without adding a persistence model."""

    def __init__(
        self,
        runtime,
        *,
        phase6_enabled: bool,
        master_id: str,
        control_enabled: bool = True,
        activity_limit: int = 30,
    ) -> None:
        self.runtime = runtime
        self.phase6_enabled = phase6_enabled
        self.master_id = master_id
        self.control_enabled = control_enabled
        self.activity_limit = activity_limit
        self.started_at = datetime.now(timezone.utc)

    def snapshot(self) -> dict[str, Any]:
        """Return one JSON-safe, point-in-time view of the running system."""

        goals = self.runtime.control_store.goals()
        active_goals = [goal for goal in goals if goal.status.value == "ACTIVE"]
        intentions = get_intentions(self.runtime)
        active_intentions = [item for item in intentions if not item.status.terminal]
        works = self.runtime.get_work_requirements()
        processes = self.runtime.process_store.all_instances()
        providers = self.runtime.provider_store.all_providers()
        reviews = self._reviews(processes)
        self_view = project_self(self.runtime) if self.phase6_enabled else None
        master_view = (
            project_master(self.runtime, self.master_id) if self.phase6_enabled else None
        )
        llm = self._llm_status()
        failed = self._failed_processes(processes, works)
        blocked = [work for work in works if work.status in BLOCKED_WORK]
        questions = list(self_view.unresolved_questions) if self_view else []
        unavailable = [provider for provider in providers if not provider.operational]
        needs = self._needs_attention(
            reviews=reviews,
            blocked=blocked,
            failed=failed,
            questions=questions,
            unavailable=unavailable,
            llm=llm,
        )
        focus = self._focus(self_view, active_intentions)
        error_count = sum(item["severity"] == "error" for item in needs)
        warning_count = sum(item["severity"] == "warning" for item in needs)

        return {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "overview": {
                "runtime": {
                    "status": "ONLINE",
                    "started_at": _iso(self.started_at),
                    "delivery": _json_safe(self.runtime.get_delivery_health()),
                },
                "llm": llm,
                "phase6": {"enabled": self.phase6_enabled},
                "counts": {
                    "active_goals": len(active_goals),
                    "active_intentions": len(active_intentions),
                    "running_work": sum(work.status in ACTIVE_WORK for work in works),
                    "pending_reviews": len(reviews),
                    "errors": error_count,
                    "warnings": warning_count,
                },
                "focus": focus,
            },
            "needs_attention": needs,
            "goals": [self._goal(goal, intentions, works) for goal in goals],
            "intentions": [self._intention(item, goals) for item in intentions],
            "being": self._being(self_view, master_view, active_intentions),
            "activities": self._activities(processes, works),
            "work": {
                "active": [self._work(item) for item in works if item.status in ACTIVE_WORK],
                "blocked": [self._work(item) for item in blocked],
                "completed": [
                    self._work(item)
                    for item in works
                    if item.status in {WorkStatus.SATISFIED, WorkStatus.CANCELLED}
                ][-50:],
            },
            "reviews": reviews,
            "providers": [self._provider(item) for item in providers],
            "system": {
                "phase6_enabled": self.phase6_enabled,
                "delivery": _json_safe(self.runtime.get_delivery_health()),
                "process_counts": _counts(item.status.value for item in processes),
                "work_counts": _counts(item.status.value for item in works),
                "recent_failures": [self._process_failure(item) for item in failed],
                "raw_trace_available": True,
            },
            "control": {
                "enabled": self.control_enabled,
                "endpoint": "/control",
                "boundary": "Phase 5G Control Plane",
            },
        }

    def _llm_status(self) -> dict[str, Any]:
        backend = self.runtime.backends.get("llm")
        if backend is None:
            return {"enabled": False, "status": "DISABLED", "last_error": None}
        row = self.runtime.db.query_one(
            "SELECT success, error, model, created_at FROM llm_invocations "
            "ORDER BY created_at DESC LIMIT 1"
        )
        status = "READY"
        last_error = None
        last_checked = None
        if row is not None:
            status = "HEALTHY" if bool(row["success"]) else "DEGRADED"
            last_error = row["error"]
            last_checked = row["created_at"]
        return {
            "enabled": True,
            "status": status,
            "provider": getattr(backend, "provider", backend.__class__.__name__),
            "model": getattr(backend, "model", row["model"] if row else None),
            "base_url": getattr(backend, "base_url", None),
            "last_checked": last_checked,
            "last_error": last_error,
        }

    def _focus(self, self_view, intentions) -> dict[str, Any]:
        attention = self.runtime.state_store.get_current("self", "attention")
        if attention is not None and isinstance(attention.value, dict):
            return {
                "label": attention.value.get("reason") or "Attention is active",
                "disposition": attention.value.get("disposition"),
                "source_event_type": attention.value.get("source_event_type"),
                "updated_at": _iso(attention.updated_at),
            }
        active = [item for item in intentions if item.status is IntentionStatus.ACTIVE]
        if active:
            return {
                "label": active[0].focus,
                "disposition": "INTENTION",
                "source_event_type": None,
                "updated_at": _iso(active[0].updated_at),
            }
        questions = list(self_view.unresolved_questions) if self_view else []
        if questions:
            return {
                "label": "Unresolved Self question requires attention",
                "disposition": "INVESTIGATE",
                "source_event_type": None,
                "updated_at": None,
            }
        return {
            "label": "Waiting for the next relevant Event",
            "disposition": "WAITING",
            "source_event_type": None,
            "updated_at": None,
        }

    def _reviews(self, processes) -> list[dict[str, Any]]:
        process_by_id = {item.id: item for item in processes}
        reviews: list[dict[str, Any]] = []
        for continuation in self.runtime.continuation_store.all():
            conditions = continuation.waiting_for.get("any") or [continuation.waiting_for]
            for condition in conditions:
                if not isinstance(condition, dict):
                    continue
                event_type = str(condition.get("event_type") or "")
                if not event_type.endswith("_reviewed"):
                    continue
                identifiers = [
                    str(value) for key, value in condition.items() if key.endswith("_id")
                ]
                process = process_by_id.get(continuation.process_instance_id)
                review_id = identifiers[0] if identifiers else str(continuation.id)
                category = (
                    "capability_acquisition"
                    if any(
                        token in event_type
                        for token in ("extension", "construction", "installation", "acquisition")
                    )
                    else "review"
                )
                reviews.append(
                    {
                        "id": review_id,
                        "continuation_id": str(continuation.id),
                        "event_type": event_type,
                        "category": category,
                        "process": process.definition_name if process else None,
                        "created_at": _iso(continuation.created_at),
                        "condition": _json_safe(condition),
                        "summary": _review_summary(event_type),
                    }
                )
        return sorted(reviews, key=lambda item: item["created_at"] or "")

    def _needs_attention(self, *, reviews, blocked, failed, questions, unavailable, llm):
        items: list[dict[str, Any]] = []
        for review in reviews:
            items.append(
                {
                    "kind": review["category"],
                    "severity": "warning",
                    "title": review["summary"],
                    "message": "NEXUS SEED is waiting for an authorized human decision.",
                    "target_id": review["id"],
                    "raw": review,
                }
            )
        for work in blocked:
            items.append(
                {
                    "kind": "blocked_work",
                    "severity": "error",
                    "title": f"Work is blocked: {work.objective or work.work_type}",
                    "message": _blocked_work_message(work.status),
                    "target_id": str(work.id),
                    "raw": self._work(work),
                }
            )
        for process in failed[-10:]:
            error = humanize_error(process.last_error, source=process.definition_name)
            items.append(
                {
                    "kind": "process_error",
                    "severity": error["severity"],
                    "title": error["title"],
                    "message": error["message"],
                    "target_id": str(process.id),
                    "raw": self._process_failure(process),
                }
            )
        if llm.get("status") == "DEGRADED" and not any(
            item["kind"] == "process_error" and "LLM" in item["title"] for item in items
        ):
            error = humanize_error(llm.get("last_error"), source="llm")
            items.append(
                {
                    "kind": "llm_error",
                    "severity": error["severity"],
                    "title": error["title"],
                    "message": error["message"],
                    "target_id": None,
                    "raw": llm,
                }
            )
        for question in questions:
            items.append(
                {
                    "kind": "self_question",
                    "severity": "warning",
                    "title": "NEXUS SEED has an unresolved question",
                    "message": _question_text(question),
                    "target_id": _question_id(question),
                    "raw": _json_safe(question),
                }
            )
        for provider in unavailable:
            items.append(
                {
                    "kind": "provider_error",
                    "severity": "warning",
                    "title": f"Provider {provider.name} is not operational",
                    "message": f"Status {provider.status.value}, health {provider.health.value}.",
                    "target_id": str(provider.id),
                    "raw": self._provider(provider),
                }
            )
        order = {"error": 0, "warning": 1, "info": 2}
        return sorted(items, key=lambda item: (order[item["severity"]], item["title"]))

    def _failed_processes(self, processes, works):
        work_by_id = {work.id: work for work in works}
        failed = []
        for process in processes:
            if process.status is not ProcessStatus.FAILED:
                continue
            work = work_by_id.get(process.work_requirement_id)
            if work is not None and work.resolved:
                continue
            failed.append(process)
        return sorted(failed, key=lambda item: item.updated_at)

    def _goal(self, goal, intentions, works) -> dict[str, Any]:
        intention = next((item for item in intentions if item.goal_id == goal.id), None)
        related = [work for work in works if work.goal_id == goal.id]
        return {
            "id": str(goal.id),
            "title": goal.title,
            "objective": goal.objective,
            "status": goal.status.value,
            "priority": goal.priority.value,
            "deadline": _iso(goal.deadline),
            "updated_at": _iso(goal.updated_at),
            "intention_id": str(intention.id) if intention else None,
            "work_counts": _counts(item.status.value for item in related),
        }

    def _intention(self, intention, goals) -> dict[str, Any]:
        goal = next((item for item in goals if item.id == intention.goal_id), None)
        return {
            "id": str(intention.id),
            "goal_id": str(intention.goal_id),
            "goal_title": goal.title if goal else None,
            "focus": intention.focus,
            "status": intention.status.value,
            "priority": goal.priority.value if goal else None,
            "reason": intention.reason,
            "reconsider_on": list(intention.reconsider_on),
            "work_requirement_ids": [str(value) for value in intention.work_requirement_ids],
            "updated_at": _iso(intention.updated_at),
        }

    def _being(self, self_view, master_view, intentions) -> dict[str, Any]:
        reflection_events = self.runtime.event_store.by_type("reflection_completed")
        recent_reflection = reflection_events[-1].payload if reflection_events else None
        if self_view is None:
            self_data = {
                "identity": None,
                "current_concerns": [],
                "commitments": [],
                "unresolved_questions": [],
                "capabilities": [],
                "recent_reflection": None,
            }
        else:
            self_data = {
                "identity": _json_safe(self_view.identity),
                "current_concerns": _json_safe(self_view.current_concerns),
                "commitments": _json_safe(self_view.commitments),
                "unresolved_questions": [
                    {
                        "id": _question_id(question),
                        "text": _question_text(question),
                        "raw": _json_safe(question),
                    }
                    for question in self_view.unresolved_questions
                ],
                "beliefs": _json_safe(self_view.beliefs),
                "capabilities": list(self_view.available_capabilities),
                "recent_reflection": _json_safe(recent_reflection),
            }
        master_data: dict[str, Any] = {"master_id": self.master_id, "claims": {}}
        if master_view is not None:
            for category in (
                "goals",
                "preferences",
                "projects",
                "commitments",
                "concerns",
                "shared_history",
            ):
                master_data["claims"][category] = [
                    {
                        "key": claim.key,
                        "value": _json_safe(claim.value),
                        "status": claim.status.value,
                        "confidence": claim.confidence,
                        "reason": claim.reason,
                        "source_event_id": (
                            str(claim.source_event_id) if claim.source_event_id else None
                        ),
                    }
                    for claim in getattr(master_view, category)
                ]
        return {
            "self": self_data,
            "master": master_data,
            "intentions": [self._intention(item, self.runtime.control_store.goals()) for item in intentions],
            "claim_statuses": [status.value for status in ClaimStatus],
        }

    def _work(self, work) -> dict[str, Any]:
        return {
            "id": str(work.id),
            "type": work.work_type,
            "objective": work.objective,
            "status": work.status.value,
            "priority": work.human_priority or work.priority,
            "reason": work.reason,
            "goal_id": str(work.goal_id) if work.goal_id else None,
            "missing_capabilities": list(work.missing_capabilities),
            "provider_directive": _json_safe(work.provider_directive),
            "deadline": _iso(work.deadline),
            "updated_at": _iso(work.updated_at),
        }

    @staticmethod
    def _provider(provider) -> dict[str, Any]:
        return {
            "id": str(provider.id),
            "name": provider.name,
            "version": provider.version,
            "kind": provider.kind.value,
            "status": provider.status.value,
            "health": provider.health.value,
            "operational": provider.operational,
            "priority": provider.priority,
            "trust_level": provider.trust_level,
            "updated_at": _iso(provider.updated_at),
        }

    @staticmethod
    def _process_failure(process) -> dict[str, Any]:
        return {
            "id": str(process.id),
            "definition": f"{process.definition_name}@{process.definition_version}",
            "status": process.status.value,
            "error": process.last_error,
            "retry_count": process.retry_count,
            "max_retries": process.max_retries,
            "updated_at": _iso(process.updated_at),
        }

    def _activities(self, processes, works) -> list[dict[str, Any]]:
        events = self.runtime.event_store.recent(300)
        deltas = self.runtime.state_delta_store.all()[-300:]
        proposals = self.runtime.action_proposal_store.all()[-200:]
        executions = self.runtime.action_execution_store.all()[-200:]
        event_by_id = {event.id: event for event in events}
        groups: dict[str, dict[str, list[Any]]] = defaultdict(
            lambda: {
                "events": [],
                "processes": [],
                "deltas": [],
                "works": [],
                "proposals": [],
                "executions": [],
            }
        )

        def event_key(event) -> str:
            return str(event.correlation_id or event.id)

        for event in events:
            groups[event_key(event)]["events"].append(event)
        for process in processes[-500:]:
            trigger = event_by_id.get(process.trigger_event_id)
            key = event_key(trigger) if trigger else str(process.trigger_event_id or process.id)
            groups[key]["processes"].append(process)
        for delta in deltas:
            source = event_by_id.get(delta.source_event_id)
            key = event_key(source) if source else str(delta.source_event_id or delta.id)
            groups[key]["deltas"].append(delta)
        for work in works[-300:]:
            source = event_by_id.get(work.source_event_id)
            key = event_key(source) if source else str(work.source_event_id or work.id)
            groups[key]["works"].append(work)
        proposal_by_id = {proposal.id: proposal for proposal in proposals}
        for proposal in proposals:
            trigger = event_by_id.get(proposal.trigger_event_id)
            key = event_key(trigger) if trigger else str(proposal.trigger_event_id or proposal.id)
            groups[key]["proposals"].append(proposal)
        for execution in executions:
            proposal = proposal_by_id.get(execution.action_proposal_id)
            trigger = event_by_id.get(proposal.trigger_event_id) if proposal else None
            key = event_key(trigger) if trigger else str(execution.action_proposal_id)
            groups[key]["executions"].append(execution)

        activities = [self._activity(key, value) for key, value in groups.items()]
        activities.sort(key=lambda item: item["updated_at"] or "", reverse=True)
        return activities[: self.activity_limit]

    def _activity(self, key: str, group: dict[str, list[Any]]) -> dict[str, Any]:
        events = sorted(group["events"], key=lambda item: item.occurred_at)
        processes = sorted(group["processes"], key=lambda item: item.created_at)
        deltas = group["deltas"]
        works = group["works"]
        proposals = group["proposals"]
        executions = group["executions"]
        root = next((event for event in events if event.causation_id is None), None)
        root = root or (events[0] if events else None)
        steps: list[str] = []
        if root is not None:
            steps.append(_event_step(root.type))
        names = {process.definition_name for process in processes}
        if "attention_evaluation" in names:
            steps.append("重要性と現在の関心との関連を評価")
        if "maintain_intention" in names or "evaluate_goal" in names:
            steps.append("Goal / Intentionへの影響を確認")
        if deltas or "apply_state_delta" in names:
            steps.append(f"World Stateを{len(deltas) or 1}件更新")
        if works:
            steps.append(f"必要なWorkを{len(works)}件評価")
        if proposals or executions:
            succeeded = sum(
                execution.status is ActionExecutionStatus.SUCCEEDED for execution in executions
            )
            steps.append(
                f"Actionを{len(proposals)}件検討"
                + (f"、{succeeded}件実行" if succeeded else "")
            )
        if not steps:
            steps.append("内部状態を確認")

        failed_processes = [item for item in processes if item.status is ProcessStatus.FAILED]
        failed_actions = [
            item for item in executions if item.status is ActionExecutionStatus.FAILED
        ]
        blocked_work = [item for item in works if item.status in BLOCKED_WORK]
        active = [item for item in processes if item.status in RUNNING_PROCESSES]
        if failed_processes or failed_actions:
            result = "Failed"
        elif blocked_work:
            result = "Needs attention"
        elif active:
            result = "In progress"
        else:
            result = "Completed"
            if not works and not deltas and not executions:
                steps.append("追加対応なし")
        timestamps = [
            *[item.occurred_at for item in events],
            *[item.updated_at for item in processes],
            *[item.updated_at for item in works],
            *[item.created_at for item in deltas],
        ]
        title = _activity_title(root, deltas, works)
        raw_errors = [item.last_error for item in failed_processes if item.last_error]
        raw_errors.extend(item.error for item in failed_actions if item.error)
        return {
            "id": key,
            "title": title,
            "result": result,
            "steps": steps,
            "updated_at": _iso(max(timestamps)) if timestamps else None,
            "source_event": (
                {
                    "id": str(root.id),
                    "type": root.type,
                    "source": root.source,
                    "payload": _json_safe(root.payload),
                }
                if root
                else None
            ),
            "details": {
                "events": [
                    {
                        "id": str(item.id),
                        "type": item.type,
                        "source": item.source,
                        "occurred_at": _iso(item.occurred_at),
                    }
                    for item in events
                ],
                "processes": [
                    {
                        "id": str(item.id),
                        "definition": f"{item.definition_name}@{item.definition_version}",
                        "status": item.status.value,
                        "error": item.last_error,
                    }
                    for item in processes
                ],
                "state_deltas": [
                    {
                        "id": str(item.id),
                        "entity": item.entity,
                        "attribute": item.attribute,
                        "old_value": _json_safe(item.old_value),
                        "new_value": _json_safe(item.new_value),
                        "reason": item.reason,
                    }
                    for item in deltas
                ],
                "work": [self._work(item) for item in works],
                "actions": [
                    {
                        "id": str(item.id),
                        "type": item.action_type,
                        "backend": item.backend,
                        "status": item.status.value,
                        "risk": item.risk_level.value,
                    }
                    for item in proposals
                ],
                "raw_errors": raw_errors,
            },
        }


def humanize_error(raw_error: str | None, *, source: str = "") -> dict[str, str]:
    """Translate an internal error while preserving its raw text separately."""

    raw = str(raw_error or "Unknown internal error")
    lowered = raw.lower()
    source_lower = source.lower()
    if "schema validation failed" in lowered or "proposed_state_deltas" in lowered:
        return {
            "severity": "error",
            "title": "LLMの解釈結果を採用できませんでした",
            "message": (
                "必要な構造化データが返されませんでした。Runtimeと安全境界は正常で、"
                "不正なWorld State更新は行われていません。"
            ),
            "raw_error": raw,
        }
    if "backend failure" in lowered or "timeout" in lowered or "cannot reach" in lowered:
        return {
            "severity": "error",
            "title": "LLMまたはProviderへ接続できませんでした",
            "message": "Runtimeは稼働中です。接続先とProviderの状態を確認してください。",
            "raw_error": raw,
        }
    if "capability" in lowered or "provider" in lowered:
        return {
            "severity": "warning",
            "title": "必要な実行能力を現在利用できません",
            "message": "要求は保持されています。CapabilityまたはProviderの回復を待っています。",
            "raw_error": raw,
        }
    if "conflict" in lowered or "permission" in lowered or "policy" in lowered:
        return {
            "severity": "warning",
            "title": "安全境界が処理を停止しました",
            "message": "不整合または権限条件を検出したため、状態変更やActionは適用されていません。",
            "raw_error": raw,
        }
    label = "LLM処理" if "llm" in source_lower else "内部処理"
    return {
        "severity": "error",
        "title": f"{label}を完了できませんでした",
        "message": "Runtimeは継続稼働しています。詳細のraw errorを確認してください。",
        "raw_error": raw,
    }


def _question_id(question: Any) -> str:
    from ..presence.models import self_question_id

    return self_question_id(question)


def _question_text(question: Any) -> str:
    if isinstance(question, dict):
        return str(question.get("question") or question.get("text") or question)
    return str(question)


def _review_summary(event_type: str) -> str:
    if "action" in event_type:
        return "Actionの実行可否を判断してください"
    if "interpretation" in event_type:
        return "LLMの解釈を確認してください"
    if any(token in event_type for token in ("extension", "construction", "installation")):
        return "Capability acquisitionの安全レビューが必要です"
    if "work" in event_type:
        return "Work開始前の確認が必要です"
    return "人間によるレビューが必要です"


def _blocked_work_message(status: WorkStatus) -> str:
    return {
        WorkStatus.BLOCKED_CAPABILITY: "必要なCapabilityがまだありません。要求自体は保持されています。",
        WorkStatus.BLOCKED_PROVIDER: "実行可能なProviderがありません。要求自体は保持されています。",
        WorkStatus.BLOCKED_PLAN: "安全に実行できるProcess planを確定できませんでした。",
    }[status]


def _event_step(event_type: str) -> str:
    return {
        "human_message": "Masterから新しいメッセージを受信",
        "external_event": "外部Eventを観測",
        "external_signal": "外部Signalを観測",
        "measurement_completed": "Measurement完了を観測",
        "goal_created": "Goalが作成された",
        "existence_wakeup": "再起動後の永続状態を復元",
    }.get(event_type, f"{event_type.replace('_', ' ')} を観測")


def _activity_title(root, deltas, works) -> str:
    if root is not None:
        text = root.payload.get("text") if isinstance(root.payload, dict) else None
        if root.type == "human_message" and text:
            compact = str(text).strip().replace("\n", " ")
            return compact[:72] + ("…" if len(compact) > 72 else "")
        return _event_step(root.type)
    if deltas:
        return f"{deltas[0].entity}.{deltas[0].attribute} の状態更新"
    if works:
        return works[0].objective or works[0].work_type
    return "Internal activity"


def _counts(values) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[str(value)] = result.get(str(value), 0) + 1
    return result


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _json_safe(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


__all__ = ["CockpitService", "humanize_error"]
