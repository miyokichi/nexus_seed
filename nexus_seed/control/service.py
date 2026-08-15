"""Channel-independent Phase 5G ConsoleService and authorization boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
import uuid

from ..autonomy.models import AcquisitionStatus
from ..backends.base import BackendRequest
from ..capabilities.models import CapabilityRequirement
from ..core.event import Event, utcnow
from ..core.process import ProcessStatus
from ..work.work_requirement import WorkRequirement, WorkStatus
from .models import (
    Command,
    CommandProposal,
    CommandResult,
    CommandStatus,
    Goal,
    GoalStatus,
    ProviderDirective,
    ProviderDirectiveKind,
    StructuredWorkRequest,
    WorkConstraints,
    WorkPriority,
)
from .parser import CommandParseError, parse_explicit_command


class CommandValidationError(ValueError):
    """Raised before execution when schema, target, or safety input is invalid."""


PERMISSIONS = {
    "task.create": "command.work.create",
    "system.status": "command.system.status",
    "work.show": "command.work.read",
    "goals.list": "command.goal.read",
    "work.pause": "command.work.pause",
    "work.resume": "command.work.resume",
    "work.cancel": "command.work.cancel",
    "work.priority": "command.work.priority",
    "work.deadline": "command.work.deadline",
    "work.provider": "command.provider.override",
    "review.approve": "command.review.approve",
    "review.reject": "command.review.approve",
    "work.context": "command.context.read",
    "work.trace": "command.trace.read",
    "goal.create": "command.goal.create",
    "goal.show": "command.goal.read",
    "goal.pause": "command.goal.pause",
    "goal.resume": "command.goal.resume",
    "goal.cancel": "command.goal.cancel",
    "goal.evaluate": "command.goal.evaluate",
}


class ConsoleService:
    """Parse, validate, authorize, execute, and audit human commands."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.db = runtime.db
        self.store = runtime.control_store

    def execute_text(
        self,
        text: str,
        *,
        issuer_identity_id: str,
        source_channel: str = "cli",
        source_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        """Execute an explicit command; natural-language text is refused."""

        command = parse_explicit_command(
            text,
            issuer_identity_id=issuer_identity_id,
            source_channel=source_channel,
            source_message_id=source_message_id,
            idempotency_key=idempotency_key,
        )
        return self.execute(command)

    def execute(
        self, command: Command, *, allow_waiting_confirmation: bool = False
    ) -> CommandResult:
        """Run a pre-parsed command through validation and authorization."""

        existing = self.store.command_by_idempotency_key(command.idempotency_key)
        if existing is not None:
            if allow_waiting_confirmation and existing.status is CommandStatus.WAITING_CONFIRMATION:
                existing.status = CommandStatus.RECEIVED
                existing.target_type = command.target_type
                existing.target_id = command.target_id
                existing.arguments = dict(command.arguments)
                command = existing
            else:
                result = self.store.get_result(existing.id)
                if result is not None:
                    return result
                return CommandResult(
                    existing.id, existing.status, message="command already received and is still pending"
                )
        else:
            self.store.save_command(command)
        identity = self.store.get_identity(command.issuer_identity_id)
        permission = PERMISSIONS.get(command.command_type)
        if identity is None or permission is None or not identity.allows(permission):
            reason = "unauthenticated or missing permission " + str(permission)
            return self._finish(command, CommandStatus.REJECTED, failure_reason=reason)
        try:
            self._validate(command)
            command.status = CommandStatus.VALIDATED
            command.validation_reasons = [
                "schema and target validated",
                f"authorized by {permission}",
            ]
            self.store.update_command(command)
            with self.db.atomic():
                result = self._dispatch(command)
                command.status = result.status
                command.executed_at = utcnow()
                self.store.update_command(command)
                self.store.save_result(result)
            return result
        except (CommandValidationError, ValueError, KeyError) as exc:
            return self._finish(command, CommandStatus.REJECTED, failure_reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - command failures must remain audited
            return self._finish(command, CommandStatus.FAILED, failure_reason=str(exc))

    def execute_proposal(
        self,
        proposal: CommandProposal,
        *,
        issuer_identity_id: str,
        confirmed: bool = False,
        source_channel: str = "conversation",
    ) -> CommandResult:
        """Validate an untrusted LLM proposal; never commit it directly."""

        if not proposal.target_id and len(proposal.candidate_target_ids) != 1:
            command = Command(
                proposal.proposed_command_type, issuer_identity_id, source_channel,
                str(proposal.id), proposal.target_type, None, proposal.arguments,
                status=CommandStatus.NEEDS_CLARIFICATION,
                idempotency_key=f"proposal:{proposal.id}",
            )
            self.store.save_command(command)
            return self._finish(
                command, CommandStatus.NEEDS_CLARIFICATION,
                failure_reason="target is ambiguous",
                data={"candidate_target_ids": list(proposal.candidate_target_ids)},
            )
        high_impact = proposal.proposed_command_type in {
            "work.cancel", "goal.cancel", "review.approve", "review.reject"
        }
        target_id = proposal.target_id or proposal.candidate_target_ids[0]
        command = Command(
            proposal.proposed_command_type, issuer_identity_id, source_channel,
            str(proposal.id), proposal.target_type, target_id, dict(proposal.arguments),
            idempotency_key=f"proposal:{proposal.id}",
        )
        if high_impact and not confirmed:
            self.store.save_command(command)
            return self._finish(
                command, CommandStatus.WAITING_CONFIRMATION,
                message="confirmation required before executing proposed high-impact command",
            )
        return self.execute(command, allow_waiting_confirmation=confirmed)

    async def propose_natural_language(
        self,
        text: str,
        *,
        candidate_targets: list[dict[str, str]],
    ) -> CommandProposal:
        """Ask the configured LLM for an inert CommandProposal.

        The model only sees an explicit target shortlist and cannot mutate any
        store.  The returned object still has to pass :meth:`execute_proposal`,
        target validation, authorization and possible confirmation.
        """

        backend = self.runtime.backends.get("llm")
        if backend is None:
            raise CommandValidationError("LLM backend is not configured")
        result = await backend.execute(
            BackendRequest(
                instruction=(
                    "Interpret the human control intent as one command proposal. "
                    "Choose no target when the shortlist is ambiguous. Do not execute anything."
                ),
                context={"text": text, "candidate_targets": candidate_targets},
                output_schema={
                    "type": "object",
                    "required": ["command_type", "arguments", "confidence", "explanation"],
                    "properties": {
                        "command_type": {"type": "string", "enum": sorted(PERMISSIONS)},
                        "target_type": {"type": ["string", "null"]},
                        "target_id": {"type": ["string", "null"]},
                        "arguments": {"type": "object"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "explanation": {"type": "string"},
                    },
                },
                metadata={"purpose": "command_proposal"},
            )
        )
        if not result.success or not isinstance(result.parsed_output, dict):
            raise CommandValidationError(result.error or "LLM did not return a command proposal")
        data = result.parsed_output
        command_type = str(data.get("command_type") or "")
        if command_type not in PERMISSIONS:
            raise CommandValidationError("LLM proposed an unsupported command type")
        confidence = float(data.get("confidence", -1))
        if not 0 <= confidence <= 1:
            raise CommandValidationError("proposal confidence must be between 0 and 1")
        known_ids = tuple(str(item["id"]) for item in candidate_targets if item.get("id"))
        target_id = str(data["target_id"]) if data.get("target_id") else None
        if target_id is not None and target_id not in known_ids:
            raise CommandValidationError("LLM proposed a target outside the supplied shortlist")
        return CommandProposal(
            proposed_command_type=command_type,
            target_type=str(data["target_type"]) if data.get("target_type") else None,
            target_id=target_id,
            arguments=dict(data.get("arguments") or {}),
            confidence=confidence,
            explanation=str(data.get("explanation") or ""),
            candidate_target_ids=known_ids if target_id is None else (),
        )

    def _validate(self, command: Command) -> None:
        if command.command_type not in PERMISSIONS:
            raise CommandValidationError(f"unsupported command type {command.command_type!r}")
        if command.target_type and not command.target_id:
            raise CommandValidationError(f"{command.command_type} requires a target id")
        if command.command_type == "task.create":
            self._work_request(command.arguments)
        elif command.command_type == "goal.create":
            if not str(command.arguments.get("objective") or "").strip():
                raise CommandValidationError("goal objective is required")
        elif command.target_type == "work":
            self._work(command.target_id)
        elif command.target_type == "goal":
            self._goal(command.target_id)
        elif command.target_type == "review":
            self._review(command.target_id)
        if command.command_type == "work.priority":
            WorkPriority(str(command.arguments.get("priority", "")).upper())
        if command.command_type == "work.deadline":
            self._future_datetime(command.arguments.get("deadline"))
        if command.command_type == "work.provider":
            ProviderDirectiveKind(str(command.arguments.get("kind", "")).upper())
            if not str(command.arguments.get("provider") or "").strip():
                raise CommandValidationError("provider name/id is required")

    def _dispatch(self, command: Command) -> CommandResult:
        handlers = {
            "task.create": self._create_work,
            "system.status": self._status,
            "work.show": self._show_work,
            "goals.list": self._list_goals,
            "work.pause": self._pause_work,
            "work.resume": self._resume_work,
            "work.cancel": self._cancel_work,
            "work.priority": self._priority,
            "work.deadline": self._deadline,
            "work.provider": self._provider,
            "review.approve": self._review_decision,
            "review.reject": self._review_decision,
            "work.context": self._context,
            "work.trace": self._trace,
            "goal.create": self._create_goal,
            "goal.show": self._show_goal,
            "goal.pause": self._goal_status,
            "goal.resume": self._goal_status,
            "goal.cancel": self._goal_status,
            "goal.evaluate": self._evaluate_goal,
        }
        return handlers[command.command_type](command)

    def _create_work(self, command: Command) -> CommandResult:
        request = self._work_request(command.arguments)
        digest = hashlib.sha256(command.idempotency_key.encode()).hexdigest()[:24]
        work_type = str(request.metadata.get("work_type") or "human_requested_work")
        capabilities = request.metadata.get("required_capabilities") or ["fulfill_human_request"]
        entities = request.scope.get("entities") or ([request.project] if request.project else [])
        requirement = WorkRequirement(
            work_type=work_type, work_key=f"human:{digest}", related_entities=list(entities),
            reason=f"explicit human command: {request.objective}",
            priority=request.priority.scheduling_value,
            metadata={**request.metadata, "source": "human_command"},
            required_capabilities=[CapabilityRequirement(str(name)) for name in capabilities],
            available_input_types=list(request.metadata.get("available_input_types") or ()),
            required_output_types=list(request.metadata.get("required_output_types") or ()),
            objective=request.objective, scope=request.scope, project=request.project,
            human_priority=request.priority.value, deadline=request.deadline,
            input_resources=list(request.input_resources), constraints=request.constraints.to_dict(),
            completion_criteria=list(request.completion_criteria),
            provider_directive=request.provider_directive.to_dict() if request.provider_directive else None,
            command_id=command.id,
        )
        review_required = request.constraints.human_review_required
        if review_required:
            requirement.status = WorkStatus.WAITING_REVIEW
        event = self._event(
            "work_review_required" if review_required else "work_required",
            command,
            {"work_requirement_id": str(requirement.id)},
        )
        requirement.source_event_id = event.id
        self.runtime.work_requirement_store.save(requirement)
        command.target_type = "work"
        command.target_id = str(requirement.id)
        return self._executed(
            command, "work created", [("work", requirement.id)], [event],
            data=self._work_data(requirement),
        )

    def _status(self, command: Command) -> CommandResult:
        works = self.runtime.get_work_requirements()
        active_statuses = {WorkStatus.EXPECTED, WorkStatus.MATCHED, WorkStatus.SPAWNED, WorkStatus.PLANNED}
        blocked_statuses = {WorkStatus.BLOCKED_CAPABILITY, WorkStatus.BLOCKED_PLAN, WorkStatus.BLOCKED_PROVIDER}
        providers = self.runtime.provider_store.all_providers()
        sessions = self.runtime.autonomy_store.sessions() if self.runtime.autonomy_store else []
        data = {
            "active_work": sum(item.status in active_statuses for item in works),
            "paused_work": sum(item.status is WorkStatus.PAUSED for item in works),
            "blocked_work": sum(item.status in blocked_statuses for item in works),
            "waiting_reviews": len(self._reviews()),
            "active_capability_acquisitions": sum(not item.status.terminal for item in sessions),
            "unavailable_providers": [str(item.id) for item in providers if not item.operational],
            "outstanding_event_deliveries": self.runtime.get_pending_event_delivery_count(),
            "system_health": self.runtime.get_delivery_health(),
        }
        return self._executed(command, "system status", [], [], data=data)

    def _show_work(self, command: Command) -> CommandResult:
        work = self._work(command.target_id)
        return self._executed(command, "work detail", [("work", work.id)], [], data=self._work_data(work, detail=True))

    def _pause_work(self, command: Command) -> CommandResult:
        work = self._work(command.target_id)
        if not self.runtime.work_requirement_store.pause(work.id):
            raise CommandValidationError(f"work cannot be paused from {work.status.value}")
        self.runtime.process_store.pause_for_work(work.id)
        event = self._event("work_paused", command, {"work_requirement_id": str(work.id)})
        return self._executed(command, "work paused", [("work", work.id)], [event])

    def _resume_work(self, command: Command) -> CommandResult:
        work = self._work(command.target_id)
        restored = self.runtime.work_requirement_store.resume(work.id)
        if restored is None:
            raise CommandValidationError("work is not paused")
        self.runtime.process_store.resume_for_work(work.id)
        event = self._event(
            "work_resumed", command,
            {"work_requirement_id": str(work.id), "restored_status": restored.value},
        )
        events = [event]
        for row in self.db.query(
            "SELECT id, status, root_proposal_id FROM action_proposals WHERE source_work_requirement_id=? AND status IN ('PENDING','APPROVED')",
            (str(work.id),),
        ):
            event_type = "action_proposed" if row["status"] == "PENDING" else "action_approved"
            events.append(self._event(event_type, command, {
                "action_proposal_id": row["id"],
                "root_proposal_id": row["root_proposal_id"] or row["id"],
            }))
        return self._executed(command, "work resumed with fresh activation context", [("work", work.id)], events)

    def _cancel_work(self, command: Command) -> CommandResult:
        work = self._work(command.target_id)
        if work.resolved:
            raise CommandValidationError(f"work is already {work.status.value}")
        self.runtime.work_requirement_store.update_status(work.id, WorkStatus.CANCELLED)
        self.runtime.process_store.pause_for_work(work.id)
        for session in self.runtime.autonomy_store.sessions_for_work(work.id):
            if not session.status.terminal:
                session.status = AcquisitionStatus.CANCELLED
                session.blocked_reason = "CANCELLED_BY_HUMAN_COMMAND"
                session.completed_at = utcnow()
                self.runtime.autonomy_store.save_session(session)
        events = [self._event("work_cancelled", command, {"work_requirement_id": str(work.id), "reason": "human command"})]
        for row in self.db.query(
            "SELECT id, root_proposal_id FROM action_proposals WHERE source_work_requirement_id=? AND status IN ('PENDING','REVIEW','APPROVED')",
            (str(work.id),),
        ):
            self.runtime.action_proposal_store.update_status(uuid.UUID(row["id"]), "REJECTED")
            events.append(self._event("action_rejected", command, {
                "action_proposal_id": row["id"],
                "root_proposal_id": row["root_proposal_id"] or row["id"],
                "reasons": ["source work cancelled by human command"],
            }))
        return self._executed(command, "work and unnecessary acquisition cancelled", [("work", work.id)], events)

    def _priority(self, command: Command) -> CommandResult:
        work = self._work(command.target_id)
        priority = WorkPriority(str(command.arguments["priority"]).upper())
        self.runtime.work_requirement_store.update_priority(work.id, priority.value, priority.scheduling_value)
        self.runtime.process_store.update_priority_for_work(work.id, priority.scheduling_value)
        event = self._event("work_priority_changed", command, {"work_requirement_id": str(work.id), "priority": priority.value})
        return self._executed(command, f"priority changed to {priority.value}", [("work", work.id)], [event])

    def _deadline(self, command: Command) -> CommandResult:
        work = self._work(command.target_id)
        deadline = self._future_datetime(command.arguments["deadline"])
        self.runtime.work_requirement_store.update_deadline(work.id, deadline)
        event = self._event("work_deadline_changed", command, {"work_requirement_id": str(work.id), "deadline": deadline.isoformat()})
        return self._executed(command, "deadline changed", [("work", work.id)], [event])

    def _provider(self, command: Command) -> CommandResult:
        work = self._work(command.target_id)
        directive = ProviderDirective(
            ProviderDirectiveKind(str(command.arguments["kind"]).upper()),
            str(command.arguments["provider"]),
        )
        self.runtime.work_requirement_store.update_provider_directive(work.id, directive.to_dict())
        event = self._event("work_provider_constraint_changed", command, {"work_requirement_id": str(work.id), **directive.to_dict()})
        return self._executed(command, "provider directive changed", [("work", work.id)], [event])

    def _review_decision(self, command: Command) -> CommandResult:
        review = self._review(command.target_id)
        decision = "approve" if command.command_type.endswith("approve") else "reject"
        payload = {key: value for key, value in review["condition"].items() if key != "event_type"}
        payload["decision"] = decision
        event = self._event(review["condition"]["event_type"], command, payload)
        return self._executed(command, f"review {decision} submitted", [("review", command.target_id)], [event])

    def _context(self, command: Command) -> CommandResult:
        work = self._work(command.target_id)
        processes = self.runtime.process_store.find_by_work_requirement_id(work.id)
        snapshots = []
        for process in processes:
            latest = self.runtime.context_snapshot_store.latest_for_instance(process.id)
            if latest:
                snapshot = latest.context_json
                snapshots.append(
                    {
                        "snapshot_id": str(latest.id),
                        "compiled_at": latest.compiled_at.isoformat(),
                        "recent_events": snapshot.get("recent_events", []),
                        "resources": snapshot.get("resources", []),
                        "world_state_references": [
                            {"entity": entity, "attribute": attribute, "version": item.get("version")}
                            for entity, attributes in snapshot.get("world_state", {}).items()
                            for attribute, item in attributes.items()
                        ],
                    }
                )
        state_refs = []
        for entity in work.related_entities:
            state_refs.extend({"entity": item.entity, "attribute": item.attribute, "version": item.version}
                              for item in self.runtime.state_store.current_for_entity(entity))
        data = {
            "world_state_references": state_refs,
            "resources": list(work.input_resources),
            "recent_context_snapshots": snapshots[-3:],
            "related_work": [str(item.id) for item in self.runtime.get_work_requirements()
                             if item.id != work.id and set(item.related_entities) & set(work.related_entities)],
            "active_constraints": work.constraints,
        }
        return self._executed(command, "current work context summary", [("work", work.id)], [], data=data)

    def _trace(self, command: Command) -> CommandResult:
        work = self._work(command.target_id)
        processes = self.runtime.process_store.find_by_work_requirement_id(work.id)
        selections = []
        invocations = []
        for process in processes:
            selections.extend(self.runtime.provider_store.selections_for_instance(process.id))
            invocations.extend(
                item for item in self.runtime.provider_store.invocations()
                if item.process_instance_id == process.id
            )
        command_rows = self.db.query("SELECT id, command_type, issuer_identity_id, created_at FROM commands WHERE target_id=? OR id=? ORDER BY created_at", (str(work.id), str(work.command_id)))
        actions = [dict(row) for row in self.db.query(
            "SELECT id, backend, action_type, target, status FROM action_proposals WHERE source_work_requirement_id=? ORDER BY created_at",
            (str(work.id),),
        )]
        action_ids = [row["id"] for row in actions]
        action_executions = []
        for action_id in action_ids:
            action_executions.extend(
                dict(row) for row in self.db.query(
                    "SELECT id, action_proposal_id, status, error FROM action_executions WHERE action_proposal_id=? ORDER BY started_at",
                    (action_id,),
                )
            )
        gaps = [dict(row) for row in self.db.query(
            "SELECT id, status, reason FROM capability_gaps WHERE work_requirement_id=? ORDER BY created_at",
            (str(work.id),),
        )]
        extensions = [dict(row) for row in self.db.query(
            "SELECT id, strategy, status, estimated_risk FROM extension_proposals WHERE work_requirement_id=? ORDER BY created_at",
            (str(work.id),),
        )]
        construction = [dict(row) for row in self.db.query(
            "SELECT id, status, attempt FROM construction_plans WHERE work_requirement_id=? ORDER BY created_at",
            (str(work.id),),
        )]
        installations = [dict(row) for row in self.db.query(
            "SELECT id, status, component_name, component_version FROM installation_plans WHERE work_requirement_id=? ORDER BY created_at",
            (str(work.id),),
        )]
        data = {
            "human_commands": [dict(row) for row in command_rows],
            "work": self._work_data(work, detail=True),
            "processes": [{"id": str(p.id), "definition": p.definition_name, "status": p.status.value} for p in processes],
            "provider_selections": [{"provider_id": str(s.provider_id) if s.provider_id else None, "reasons": s.reasons} for s in selections],
            "provider_invocations": [{"id": str(i.id), "provider_id": str(i.provider_id), "status": i.status.value} for i in invocations],
            "actions": actions,
            "action_executions": action_executions,
            "capability_gaps": gaps,
            "extensions": extensions,
            "construction": construction,
            "installations": installations,
            "resources": list(work.input_resources),
            "source_event_id": str(work.source_event_id) if work.source_event_id else None,
            "source_state_delta_id": str(work.source_state_delta_id) if work.source_state_delta_id else None,
            "selected_plan_id": str(work.selected_plan_id) if work.selected_plan_id else None,
        }
        return self._executed(command, "work provenance trace", [("work", work.id)], [], data=data)

    def _create_goal(self, command: Command) -> CommandResult:
        args = command.arguments
        goal = Goal(
            title=str(args.get("title") or args["objective"]), objective=str(args["objective"]),
            owner_identity_id=command.issuer_identity_id,
            scope=_dict(args.get("scope")), priority=WorkPriority(str(args.get("priority", "NORMAL")).upper()),
            deadline=self._optional_goal_deadline(args.get("deadline")),
            constraints=WorkConstraints.from_dict(_dict(args.get("constraints"))),
            success_criteria=list(args.get("success_criteria") or args.get("success") or ()),
            metadata=_dict(args.get("metadata")),
        )
        self.store.save_goal(goal)
        command.target_type = "goal"
        command.target_id = str(goal.id)
        event = self._event("goal_created", command, {"goal_id": str(goal.id)})
        return self._executed(command, "goal created", [("goal", goal.id)], [event], data=self._goal_data(goal))

    def _show_goal(self, command: Command) -> CommandResult:
        goal = self._goal(command.target_id)
        return self._executed(command, "goal detail", [("goal", goal.id)], [], data=self._goal_data(goal, True))

    def _list_goals(self, command: Command) -> CommandResult:
        return self._executed(command, "goals", [], [], data={"goals": [self._goal_data(g) for g in self.store.goals()]})

    def _goal_status(self, command: Command) -> CommandResult:
        goal = self._goal(command.target_id)
        action = command.command_type.split(".")[1]
        status = {"pause": GoalStatus.PAUSED, "resume": GoalStatus.ACTIVE, "cancel": GoalStatus.CANCELLED}[action]
        past = {"pause": "paused", "resume": "resumed", "cancel": "cancelled"}[action]
        if goal.status.terminal:
            raise CommandValidationError(f"goal is already {goal.status.value}")
        self.store.update_goal_status(goal.id, status)
        events = [self._event(f"goal_{past}", command, {"goal_id": str(goal.id)})]
        if status is GoalStatus.CANCELLED:
            for work in self.runtime.work_requirement_store.for_goal(goal.id):
                if not work.resolved:
                    self.runtime.work_requirement_store.update_status(work.id, WorkStatus.CANCELLED)
                    self.runtime.process_store.pause_for_work(work.id)
                    for session in self.runtime.autonomy_store.sessions_for_work(work.id):
                        if not session.status.terminal:
                            session.status = AcquisitionStatus.CANCELLED
                            session.blocked_reason = "GOAL_CANCELLED_BY_HUMAN_COMMAND"
                            session.completed_at = utcnow()
                            self.runtime.autonomy_store.save_session(session)
        elif status is GoalStatus.ACTIVE:
            events.append(self._event("goal_evaluation_requested", command, {"goal_id": str(goal.id)}))
        return self._executed(command, f"goal {past}", [("goal", goal.id)], events)

    def _evaluate_goal(self, command: Command) -> CommandResult:
        goal = self._goal(command.target_id)
        event = self._event("goal_evaluation_requested", command, {"goal_id": str(goal.id)})
        return self._executed(command, "goal evaluation requested", [("goal", goal.id)], [event])

    def _work_request(self, args: dict[str, Any]) -> StructuredWorkRequest:
        objective = str(args.get("objective") or "").strip()
        if not objective:
            raise CommandValidationError("task objective is required")
        priority = WorkPriority(str(args.get("priority", "NORMAL")).upper())
        deadline = self._optional_future_datetime(args.get("deadline"))
        constraints = _dict(args.get("constraints"))
        for name in ("cloud_forbidden", "network_forbidden", "production_write_forbidden", "human_review_required"):
            if name in args:
                constraints[name] = bool(args[name])
        directive = None
        if args.get("preferred_provider"):
            directive = ProviderDirective(ProviderDirectiveKind.PREFER, str(args["preferred_provider"]))
        metadata = _dict(args.get("metadata"))
        for name in ("work_type", "required_capabilities", "available_input_types", "required_output_types"):
            if name in args:
                metadata[name] = args[name]
        return StructuredWorkRequest(
            objective=objective, scope=_dict(args.get("scope")), project=args.get("project"),
            priority=priority, deadline=deadline,
            input_resources=tuple(str(v) for v in args.get("input_resources", ()) or ()),
            constraints=WorkConstraints.from_dict(constraints),
            completion_criteria=tuple(args.get("completion_criteria", ()) or ()),
            provider_directive=directive, metadata=metadata,
        )

    def _work(self, value) -> WorkRequirement:
        try:
            work = self.runtime.get_work_requirement(uuid.UUID(str(value)))
        except (ValueError, TypeError):
            work = None
        if work is None:
            raise CommandValidationError(f"work target {value!r} was not found or is ambiguous")
        return work

    def _goal(self, value) -> Goal:
        try:
            goal = self.store.get_goal(uuid.UUID(str(value)))
        except (ValueError, TypeError):
            goal = None
        if goal is None:
            raise CommandValidationError(f"goal target {value!r} was not found or is ambiguous")
        return goal

    def _reviews(self) -> list[dict[str, Any]]:
        reviews = []
        for continuation in self.runtime.continuation_store.all():
            conditions = continuation.waiting_for.get("any") or [continuation.waiting_for]
            for condition in conditions:
                event_type = str(condition.get("event_type") or "") if isinstance(condition, dict) else ""
                if event_type.endswith("_reviewed"):
                    ids = [str(v) for k, v in condition.items() if k.endswith("_id")]
                    reviews.append({"review_id": ids[0] if ids else str(continuation.id), "condition": condition, "continuation_id": str(continuation.id)})
        return reviews

    def _review(self, value) -> dict[str, Any]:
        matches = [item for item in self._reviews() if value in {item["review_id"], item["continuation_id"]}]
        if len(matches) != 1:
            raise CommandValidationError(f"review target {value!r} was not found or is ambiguous")
        return matches[0]

    def _work_data(self, work: WorkRequirement, detail: bool = False) -> dict[str, Any]:
        data = {
            "id": str(work.id), "objective": work.objective, "status": work.status.value,
            "priority": work.human_priority or work.priority, "deadline": work.deadline.isoformat() if work.deadline else None,
            "required_capabilities": [item.name for item in work.required_capabilities],
            "missing_capabilities": list(work.missing_capabilities),
            "selected_definition": (
                f"{work.selected_definition_name}@{work.selected_definition_version}"
                if work.selected_definition_name else None
            ),
            "selected_plan_id": str(work.selected_plan_id) if work.selected_plan_id else None,
            "provider_directive": work.provider_directive, "constraints": work.constraints,
            "goal_id": str(work.goal_id) if work.goal_id else None,
        }
        if detail:
            processes = self.runtime.process_store.find_by_work_requirement_id(work.id)
            selected_provider = None
            for process in reversed(processes):
                selections = self.runtime.provider_store.selections_for_instance(process.id)
                if selections and selections[-1].provider_id:
                    provider = self.runtime.provider_store.get_provider(selections[-1].provider_id)
                    selected_provider = provider.name if provider else str(selections[-1].provider_id)
                    break
            process_ids = {str(process.id) for process in processes}
            relevant_reviews = [
                item["review_id"] for item in self._reviews()
                if str(self.runtime.continuation_store.get(uuid.UUID(item["continuation_id"])).process_instance_id)
                in process_ids
            ]
            data.update({
                "current_stage": processes[-1].status.value if processes else work.status.value,
                "current_process": processes[-1].definition_name if processes else None,
                "selected_provider": selected_provider,
                "blocking_reason": work.reason if work.status.value.startswith("BLOCKED") else None,
                "related_resources": work.input_resources,
                "pending_reviews": relevant_reviews,
                "next_expected_step": _next_step(work),
                "completion_criteria": work.completion_criteria,
            })
        return data

    def _goal_data(self, goal: Goal, detail: bool = False) -> dict[str, Any]:
        data = {"id": str(goal.id), "title": goal.title, "objective": goal.objective,
                "status": goal.status.value, "priority": goal.priority.value,
                "deadline": goal.deadline.isoformat() if goal.deadline else None}
        if detail:
            data.update({"scope": goal.scope, "constraints": goal.constraints.to_dict(),
                         "success_criteria": goal.success_criteria,
                         "work_ids": [str(w.id) for w in self.runtime.work_requirement_store.for_goal(goal.id)]})
        return data

    def _event(self, event_type: str, command: Command, payload: dict) -> Event:
        event = Event(type=event_type, source=f"human:{command.issuer_identity_id}",
                      payload={**payload, "command_id": str(command.id)})
        self.runtime.event_store.append(event)
        return event

    def _executed(self, command, message, affected, events, *, data=None) -> CommandResult:
        return CommandResult(
            command.id, CommandStatus.EXECUTED,
            affected_entities=[{"type": kind, "id": str(identifier)} for kind, identifier in affected],
            emitted_events=[str(event.id) for event in events], message=message, data=data or {},
        )

    def _finish(self, command, status, *, message="", failure_reason=None, data=None) -> CommandResult:
        command.status = status
        command.validation_reasons = [failure_reason] if failure_reason else []
        command.executed_at = utcnow() if status in {CommandStatus.EXECUTED, CommandStatus.FAILED, CommandStatus.REJECTED} else None
        result = CommandResult(command.id, status, message=message, failure_reason=failure_reason, data=data or {})
        with self.db.atomic():
            self.store.update_command(command)
            self.store.save_result(result)
        return result

    @staticmethod
    def _future_datetime(value) -> datetime:
        if not value:
            raise CommandValidationError("deadline is required")
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise CommandValidationError("deadline must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise CommandValidationError("deadline must include a timezone")
        if parsed <= utcnow():
            raise CommandValidationError("deadline must be in the future")
        return parsed

    def _optional_future_datetime(self, value) -> datetime | None:
        return self._future_datetime(value) if value else None

    def _optional_goal_deadline(self, value) -> datetime | None:
        if not value:
            return None
        raw = str(value)
        if len(raw) == 10:
            raw += "T23:59:59" + datetime.now().astimezone().strftime("%z")
        return self._future_datetime(raw)


def _dict(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CommandValidationError("structured argument must be a JSON object")
    return dict(value)


def _next_step(work: WorkRequirement) -> str:
    return {
        WorkStatus.PAUSED: "resume by human command",
        WorkStatus.BLOCKED_CAPABILITY: "capability acquisition or registration",
        WorkStatus.BLOCKED_PROVIDER: "provider recovery or constraint change",
        WorkStatus.BLOCKED_PLAN: "replanning or human decision",
        WorkStatus.SATISFIED: "none; completion criteria satisfied",
        WorkStatus.CANCELLED: "none; cancelled",
    }.get(work.status, "continue existing work pipeline")
