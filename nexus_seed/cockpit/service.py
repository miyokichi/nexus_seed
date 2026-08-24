"""Human-facing projections for Projects, Knowledge, and Runtime health."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from ..chat.message import ProjectMessageService
from ..chat.service import ProjectChatService
from ..core.process import ProcessStatus
from ..knowledge.autonomous_loop import (
    KIND_COMPLETION_REVIEW,
    KIND_PROJECT_PROPOSAL,
    KIND_SITUATION_ASSESSMENT,
)
from ..knowledge.models import KIND_CONSOLIDATED_MEMORY, KIND_PRINCIPLE
from ..knowledge.projection import WorldStateProjection
from ..projects.projections import get_project_situation, get_project_summaries
from ..storage.orchestrator_store import A2AMessageStore, AgentStore, ProjectStore


ORCHESTRATOR_MESSAGE_LIMIT = 50


class CockpitService:
    """Compile the Project-centered Cockpit view from durable stores."""

    def __init__(self, runtime, *, master_id: str, activity_limit: int = 30) -> None:
        self.runtime = runtime
        self.master_id = master_id
        self.activity_limit = activity_limit
        self.started_at = datetime.now(timezone.utc)
        self.chat = ProjectChatService(runtime)
        self.messages = ProjectMessageService(runtime, self.chat)
        self.orchestrator_projects = ProjectStore(runtime.db)
        self.orchestrator_agents = AgentStore(runtime.db)
        self.orchestrator_messages = A2AMessageStore(runtime.db)

    def snapshot(self) -> dict[str, Any]:
        """Return one JSON-safe, point-in-time application view."""

        processes = self.runtime.process_store.all_instances()
        failed = sorted(
            (item for item in processes if item.status is ProcessStatus.FAILED),
            key=lambda item: item.updated_at,
        )
        llm = self._llm_status()
        orchestrator = self.orchestrator()
        knowledge = self.knowledge()
        live_projects = [
            item
            for item in orchestrator["projects"]
            if item["status"] not in {"COMPLETED", "CANCELLED", "FAILED"}
        ]
        pending_proposals = self._with_status(
            knowledge["proposals"], "PENDING_REVIEW"
        )
        pending_artifacts = self._with_status(
            knowledge["artifacts"], "PENDING_REVIEW"
        )
        pending_completions = self._with_status(
            knowledge["completion_reviews"], "PENDING_REVIEW"
        )
        open_questions = self._with_status(knowledge["questions"], "OPEN")
        unresolved_entities = self._with_status(
            knowledge["entities"], "UNRESOLVED"
        )
        needs = self._project_attention(
            live_projects=live_projects,
            proposals=pending_proposals,
            artifacts=pending_artifacts,
            completions=pending_completions,
            questions=open_questions,
            entities=unresolved_entities,
            failed=failed,
            llm=llm,
        )
        focus = (
            {
                "label": live_projects[0]["goal"],
                "disposition": live_projects[0]["status"],
                "source_event_type": "Project Orchestrator",
                "updated_at": live_projects[0]["updated_at"],
            }
            if live_projects
            else {
                "label": "次の観測または依頼を待っています",
                "disposition": "WAITING",
                "source_event_type": "Knowledge Runtime",
                "updated_at": None,
            }
        )
        pending_reviews = (
            len(pending_proposals) + len(pending_artifacts) + len(pending_completions)
        )
        return {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "overview": {
                "runtime": {
                    "status": "ONLINE",
                    "started_at": _iso(self.started_at),
                    "delivery": _json_safe(self.runtime.get_delivery_health()),
                },
                "llm": llm,
                "counts": {
                    "active_projects": len(live_projects),
                    "pending_reviews": pending_reviews,
                    "open_questions": len(open_questions),
                    "unresolved_entities": len(unresolved_entities),
                    "errors": sum(item["severity"] == "error" for item in needs),
                    "warnings": sum(item["severity"] == "warning" for item in needs),
                },
                "focus": focus,
            },
            "needs_attention": needs,
            "activities": self._activities(processes),
            "orchestrator": orchestrator,
            "knowledge": knowledge,
            "system": {
                "delivery": _json_safe(self.runtime.get_delivery_health()),
                "process_counts": _counts(item.status.value for item in processes),
                "recent_failures": [self._process_failure(item) for item in failed[-10:]],
                "raw_trace_available": True,
            },
        }

    @staticmethod
    def _with_status(items: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
        return [item for item in items if item["status"] == status]

    def _project_attention(
        self,
        *,
        live_projects,
        proposals,
        artifacts,
        completions,
        questions,
        entities,
        failed,
        llm,
    ) -> list[dict[str, Any]]:
        """Return Project/Knowledge decisions requiring human attention."""

        items: list[dict[str, Any]] = []
        categories = (
            ("project_proposal", "Project提案の確認が必要です", proposals),
            ("artifact_review", "成果物の確認が必要です", artifacts),
            ("completion_review", "Project完了の確認が必要です", completions),
            ("agent_question", "Agentから質問があります", questions),
            ("unknown_entity", "Entityの同一性を確認してください", entities),
        )
        for kind, title, records in categories:
            for record in records:
                items.append(
                    {
                        "kind": kind,
                        "severity": "warning",
                        "title": title,
                        "message": str(record.get("content") or record["knowledge_id"]),
                        "target_id": record["knowledge_id"],
                        "raw": record,
                    }
                )
        for project in live_projects:
            if project["blockers"]:
                items.append(
                    {
                        "kind": "project_blocked",
                        "severity": "warning",
                        "title": "Projectが停止しています",
                        "message": project["goal"],
                        "target_id": project["id"],
                        "raw": project,
                    }
                )
        for process in failed[-10:]:
            error = humanize_error(process.last_error, source=process.definition_name)
            items.append(
                {
                    "kind": "process_error",
                    "severity": "error",
                    "title": error["title"],
                    "message": error["message"],
                    "target_id": str(process.id),
                    "raw": self._process_failure(process),
                }
            )
        if llm.get("status") == "DEGRADED":
            error = humanize_error(llm.get("last_error"), source="llm")
            items.append(
                {
                    "kind": "llm_error",
                    "severity": "error",
                    "title": error["title"],
                    "message": error["message"],
                    "target_id": None,
                    "raw": llm,
                }
            )
        order = {"error": 0, "warning": 1}
        return sorted(items, key=lambda item: (order[item["severity"]], item["title"]))

    def knowledge(self) -> dict[str, Any]:
        """Return the human-facing projection of the autonomous Knowledge loop."""

        loop = getattr(self.runtime, "knowledge_loop", None)
        if loop is None:
            return {
                "enabled": False,
                "world": {"facts": [], "conflicts": []},
                "inbox": [],
                "proposals": [],
                "artifacts": [],
                "completion_reviews": [],
                "questions": [],
                "entities": [],
                "principles": [],
                "memories": [],
                "observation_sources": self._observation_sources(),
            }
        view = WorldStateProjection(loop.ledger).view()
        # What the loop derived gets its own sections below, so the inbox stays
        # what came *in* rather than mixing in what was concluded from it.
        internal_kinds = {
            KIND_PROJECT_PROPOSAL,
            KIND_SITUATION_ASSESSMENT,
            KIND_COMPLETION_REVIEW,
            KIND_PRINCIPLE,
            KIND_CONSOLIDATED_MEMORY,
        }
        inbox = [
            item for item in loop.ledger.all_heads() if item.kind not in internal_kinds
        ]
        inbox.sort(key=lambda item: item.recorded_at, reverse=True)
        return {
            "enabled": True,
            "world": {
                "facts": [
                    {
                        "entity": entity,
                        "attribute": attribute,
                        "value": _json_safe(fact.value),
                        "knowledge_id": fact.knowledge_id,
                        "confidence": fact.confidence,
                        "source": fact.source.to_dict(),
                        "recorded_at": _iso(fact.recorded_at),
                    }
                    for (entity, attribute), fact in sorted(view.facts.items())
                ],
                "conflicts": [
                    {
                        "entity": entity,
                        "attribute": attribute,
                        "claims": [
                            {
                                "value": _json_safe(fact.value),
                                "knowledge_id": fact.knowledge_id,
                                "source": fact.source.to_dict(),
                            }
                            for fact in facts
                        ],
                    }
                    for (entity, attribute), facts in sorted(view.conflicts.items())
                ],
            },
            "inbox": [self._knowledge_item(item) for item in inbox[:50]],
            "proposals": [self._knowledge_item(item) for item in loop.proposals()],
            "artifacts": [self._knowledge_item(item) for item in loop.artifacts()[:50]],
            "completion_reviews": [
                self._knowledge_item(item) for item in loop.completion_reviews()[:50]
            ],
            "questions": [
                self._knowledge_item(item) for item in loop.questions(open_only=False)
            ],
            "entities": [
                self._knowledge_item(item)
                for item in loop.entity_candidates(unresolved_only=False)
            ],
            "principles": [self._principle_item(item) for item in loop.principles()[:50]],
            "memories": [self._knowledge_item(item) for item in loop.memories()[:50]],
            "observation_sources": self._observation_sources(),
        }

    def _principle_item(self, item) -> dict[str, Any]:
        """A principle, with how much evidence stands for and against it.

        Support and counterexample counts are lifted out of metadata because
        they are the whole reason to trust — or distrust — what is written:
        a principle is a prediction that earned its status, not a fact.
        """
        metadata = item.metadata or {}
        return {
            **self._knowledge_item(item),
            "scope": metadata.get("scope"),
            "support_count": metadata.get("support_count", 0),
            "counterexample_count": metadata.get("counterexample_count", 0),
            "evidence_for": list(metadata.get("evidence_for") or []),
            "evidence_against": list(metadata.get("evidence_against") or []),
        }

    def _observation_sources(self) -> list[dict[str, Any]]:
        service = getattr(self.runtime, "observation_sources", None)
        return [item.to_dict() for item in service.all()] if service is not None else []

    async def create_observation_source(
        self,
        *,
        name: str,
        fields: list[str],
        poll_interval_seconds: float = 60.0,
    ) -> dict[str, Any]:
        """Create and immediately take the first explicitly authorized snapshot."""
        service = getattr(self.runtime, "observation_sources", None)
        if service is None:
            raise ValueError("observation sources are unavailable")
        source = service.create_system_snapshot(
            name=name,
            fields=fields,
            poll_interval_seconds=poll_interval_seconds,
        )
        outcomes = await service.poll_due(force_source_id=source.id)
        loop = getattr(self.runtime, "knowledge_loop", None)
        if loop is not None:
            await loop.reconcile()
        current = service.store.get(source.id) or source
        return {"source": current.to_dict(), "outcomes": outcomes}

    def set_observation_source_enabled(
        self, source_id: str, enabled: bool
    ) -> dict[str, Any] | None:
        """Pause or resume one durable observation source."""
        service = getattr(self.runtime, "observation_sources", None)
        if service is None:
            return None
        source = service.set_enabled(source_id, enabled)
        return source.to_dict() if source is not None else None

    async def poll_observation_source(self, source_id: str) -> dict[str, Any] | None:
        """Explicitly poll one enabled source now."""
        service = getattr(self.runtime, "observation_sources", None)
        if service is None or service.store.get(source_id) is None:
            return None
        outcomes = await service.poll_due(force_source_id=source_id)
        loop = getattr(self.runtime, "knowledge_loop", None)
        if loop is not None:
            await loop.reconcile()
        source = service.store.get(source_id)
        return {
            "source": source.to_dict() if source is not None else None,
            "outcomes": outcomes,
        }

    async def record_knowledge(
        self, text: str, *, source_event_key: str
    ) -> dict[str, Any]:
        """Record one manual observation through Ingress."""

        loop = getattr(self.runtime, "knowledge_loop", None)
        if loop is None:
            raise ValueError("knowledge loop is disabled")
        result = await loop.record_manual(
            text,
            source_event_key=source_event_key,
            metadata={"actor": self.master_id, "via": "cockpit"},
        )
        return {
            "status": result.status.value,
            "duplicate": result.duplicate,
            "event_id": str(result.event.id) if result.event else None,
            "receipt_id": str(result.receipt.id) if result.receipt else None,
        }

    async def decide_knowledge_proposal(
        self, proposal_id: str, decision: str, *, note: str = ""
    ) -> dict[str, Any] | None:
        """Approve or reject a Project proposal."""

        loop = getattr(self.runtime, "knowledge_loop", None)
        if loop is None:
            return None
        item = await loop.decide_proposal(
            proposal_id, decision, actor=self.master_id or "human", note=note
        )
        return self._knowledge_item(item) if item is not None else None

    async def answer_knowledge_question(
        self, question_id: str, answer: str
    ) -> dict[str, Any] | None:
        """Answer a question raised by a Project Agent."""

        loop = getattr(self.runtime, "knowledge_loop", None)
        if loop is None:
            return None
        item = await loop.answer_question(
            question_id, answer, actor=self.master_id or "human"
        )
        return self._knowledge_item(item) if item is not None else None

    async def decide_knowledge_artifact(
        self, artifact_id: str, decision: str, *, note: str = ""
    ) -> dict[str, Any] | None:
        """Approve an Artifact or return it to its Agent for revision."""

        loop = getattr(self.runtime, "knowledge_loop", None)
        if loop is None:
            return None
        item = await loop.decide_artifact(
            artifact_id, decision, actor=self.master_id or "human", note=note
        )
        return self._knowledge_item(item) if item is not None else None

    async def decide_knowledge_completion(
        self, review_id: str, decision: str, *, note: str = ""
    ) -> dict[str, Any] | None:
        """Approve or return one complete Agent delivery."""

        loop = getattr(self.runtime, "knowledge_loop", None)
        if loop is None:
            return None
        item = await loop.decide_completion_review(
            review_id, decision, actor=self.master_id or "human", note=note
        )
        return self._knowledge_item(item) if item is not None else None

    def decide_knowledge_entity(
        self, entity_id: str, decision: str, *, canonical_id: str | None = None
    ) -> dict[str, Any] | None:
        """Resolve one unknown Entity identity."""

        loop = getattr(self.runtime, "knowledge_loop", None)
        if loop is None:
            return None
        item = loop.decide_entity(
            entity_id,
            decision,
            canonical_id=canonical_id,
            actor=self.master_id or "human",
        )
        return self._knowledge_item(item) if item is not None else None

    @staticmethod
    def _knowledge_item(item) -> dict[str, Any]:
        return {
            "knowledge_id": item.knowledge_id,
            "revision": item.revision,
            "kind": item.kind,
            "status": item.status,
            "content": _json_safe(item.content.value),
            "source": item.source.to_dict(),
            "recorded_at": _iso(item.recorded_at),
            "derived_from": list(item.derived_from),
            "metadata": _json_safe(item.metadata),
        }

    def projects(self) -> list[dict[str, Any]]:
        """Return compact summaries of authoritative Projects."""

        return [item.to_dict() for item in get_project_summaries(self.runtime)]

    def orchestrator(self) -> dict[str, Any]:
        """Return the Project Orchestrator's durable Projects."""

        projects = self.orchestrator_projects.all()
        projects.sort(key=lambda item: (-item.priority, item.created_at))
        return {
            "enabled": getattr(self.runtime, "project_orchestrator", None) is not None,
            "counts": _counts(project.status.value for project in projects),
            "projects": [self._orchestrator_project(project) for project in projects],
        }

    def orchestrator_project(self, project_id: str) -> dict[str, Any] | None:
        """Return one Project with its Agent and audited A2A history."""

        project = self.orchestrator_projects.get(project_id)
        if project is None:
            return None
        detail = self._orchestrator_project(project)
        detail["messages"] = [
            {"direction": direction, **message.to_dict()}
            for direction, message in self.orchestrator_messages.for_project(project_id)
        ][-ORCHESTRATOR_MESSAGE_LIMIT:]
        detail["blocker_history"] = list(project.blockers)
        detail["tasks"] = list(project.tasks)
        return detail

    async def orchestrator_instruct(
        self, project_id: str, message: str, *, request_id: str | None = None
    ) -> dict[str, Any] | None:
        """Route a human instruction back into an existing Project."""

        orchestrator = getattr(self.runtime, "project_orchestrator", None)
        if orchestrator is None or self.orchestrator_projects.get(project_id) is None:
            return None
        text = (message or "").strip()
        if not text:
            raise ValueError("message must not be empty")
        decision, touched = await orchestrator.submit(
            text,
            source="cockpit-instruct",
            origin_project_id=project_id,
            request_id=request_id,
        )
        return {
            "project_id": project_id,
            "request_id": request_id,
            "decision": decision.to_dict(),
            "affected_project_id": touched.id if touched else None,
            "project": self.orchestrator_project(project_id),
        }

    async def orchestrator_unblock(
        self, project_id: str, note: str = ""
    ) -> dict[str, Any] | None:
        """Clear a Project's blockers and hand it back to its Agent."""

        orchestrator = getattr(self.runtime, "project_orchestrator", None)
        if orchestrator is None:
            return None
        project = await orchestrator.resolve_block(project_id, note=(note or "").strip())
        if project is None:
            return None
        return {"project_id": project_id, "project": self.orchestrator_project(project_id)}

    def _orchestrator_project(self, project) -> dict[str, Any]:
        agent = self.orchestrator_agents.active_for_project(project.id)
        assignment = agent.assignment if agent is not None else None
        return {
            "id": project.id,
            "goal": project.goal,
            "status": project.status.value,
            "priority": project.priority,
            "summary": project.summary,
            "assigned_agent_id": project.assigned_agent_id,
            "parent_project_id": project.parent_project_id,
            "blockers": project.current_blockers,
            "task_count": len(project.tasks),
            "created_at": _iso(project.created_at),
            "updated_at": _iso(project.updated_at),
            "agent": (
                {
                    "agent_id": agent.agent_id,
                    "runtime": agent.runtime,
                    "status": agent.status.value,
                    "endpoint": agent.endpoint,
                    "unavailable": agent.metadata.get("unavailable"),
                    "assignment": assignment.to_dict() if assignment else None,
                }
                if agent is not None
                else None
            ),
        }

    def project_situation(self, project_id: str) -> dict[str, Any] | None:
        """Return one complete Project situation."""

        situation = get_project_situation(self.runtime, project_id)
        return situation.to_dict() if situation is not None else None

    def project_chat_history(self, project_id: str) -> dict[str, Any] | None:
        """Return one Project's durable chat thread."""

        return self.chat.history(project_id)

    async def project_chat_ask(
        self, project_id: str, message: str
    ) -> dict[str, Any] | None:
        """Answer one Project question without changing the Project."""

        return await self.chat.ask(project_id, message)

    async def project_message(
        self,
        project_id: str,
        message: str,
        *,
        request_id: str | None = None,
        act: bool = False,
    ) -> dict[str, Any] | None:
        """Handle one question or instruction on a Project thread."""

        return await self.messages.send(
            project_id, message, request_id=request_id, act=act
        )

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

    def _activities(self, processes) -> list[dict[str, Any]]:
        events = self.runtime.event_store.recent(300)
        event_by_id = {event.id: event for event in events}
        groups: dict[str, dict[str, list[Any]]] = defaultdict(
            lambda: {"events": [], "processes": []}
        )
        for event in events:
            groups[str(event.correlation_id or event.id)]["events"].append(event)
        for process in processes[-500:]:
            trigger = event_by_id.get(process.trigger_event_id)
            key = str(
                (trigger.correlation_id or trigger.id)
                if trigger is not None
                else process.trigger_event_id or process.id
            )
            groups[key]["processes"].append(process)
        activities = [self._activity(key, group) for key, group in groups.items()]
        activities.sort(key=lambda item: item["updated_at"] or "", reverse=True)
        return activities[: self.activity_limit]

    @staticmethod
    def _activity(key: str, group: dict[str, list[Any]]) -> dict[str, Any]:
        events = sorted(group["events"], key=lambda item: item.occurred_at)
        processes = sorted(group["processes"], key=lambda item: item.created_at)
        root = next((event for event in events if event.causation_id is None), None)
        root = root or (events[0] if events else None)
        failed = [item for item in processes if item.status is ProcessStatus.FAILED]
        active = [
            item
            for item in processes
            if item.status
            in {
                ProcessStatus.RUNNABLE,
                ProcessStatus.RUNNING,
                ProcessStatus.RETRY_WAIT,
                ProcessStatus.SUSPENDED,
            }
        ]
        result = "Failed" if failed else "In progress" if active else "Completed"
        timestamps = [
            *[item.occurred_at for item in events],
            *[item.updated_at for item in processes],
        ]
        return {
            "id": key,
            "title": _activity_title(root),
            "result": result,
            "steps": [
                *([_event_step(root.type)] if root is not None else []),
                *(
                    [f"{len(processes)}個のProcessを実行"]
                    if processes
                    else ["追加対応なし"]
                ),
            ],
            "updated_at": _iso(max(timestamps)) if timestamps else None,
            "source_event": (
                {
                    "id": str(root.id),
                    "type": root.type,
                    "source": root.source,
                    "payload": _json_safe(root.payload),
                }
                if root is not None
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
                "raw_errors": [item.last_error for item in failed if item.last_error],
            },
        }


def humanize_error(raw_error: str | None, *, source: str = "") -> dict[str, str]:
    """Translate an internal error while preserving its raw text."""

    raw = str(raw_error or "Unknown internal error")
    lowered = raw.lower()
    if "backend failure" in lowered or "timeout" in lowered or "cannot reach" in lowered:
        title = "LLMまたはAgent Runtimeへ接続できませんでした"
        message = "Runtimeは稼働中です。接続先とAgentの状態を確認してください。"
    elif "conflict" in lowered or "permission" in lowered or "policy" in lowered:
        title = "安全境界が処理を停止しました"
        message = "不整合または権限条件を検出したため、変更は適用されていません。"
    else:
        label = "LLM処理" if "llm" in source.lower() else "内部処理"
        title = f"{label}を完了できませんでした"
        message = "Runtimeは継続稼働しています。raw errorを確認してください。"
    return {
        "severity": "error",
        "title": title,
        "message": message,
        "raw_error": raw,
    }


def _event_step(event_type: str) -> str:
    return {
        "human_message": "人から新しいメッセージを受信",
        "knowledge_observed": "新しいKnowledgeを観測",
        "file_created": "新しいResourceを観測",
        "file_modified": "Resourceの変更を観測",
    }.get(event_type, f"{event_type.replace('_', ' ')} を観測")


def _activity_title(root) -> str:
    if root is None:
        return "Internal activity"
    text = root.payload.get("text") if isinstance(root.payload, dict) else None
    if text:
        compact = str(text).strip().replace("\n", " ")
        return compact[:72] + ("…" if len(compact) > 72 else "")
    return _event_step(root.type)


def _counts(values) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[str(value)] = result.get(str(value), 0) + 1
    return result


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "to_dict"):
        return _json_safe(value.to_dict())
    return value


__all__ = ["CockpitService", "humanize_error"]
