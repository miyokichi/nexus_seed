"""One-shot MVP composition across the independently owned modules.

The flow owns only wiring.  Observer, Knowledge, Planner, Approval, Project
Manager, and the A2A Agent Runtime retain their existing responsibilities and
contracts.  In particular, no Agent implementation is imported here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ...modules.knowledge.ledger import KnowledgeLedger
from ...modules.knowledge.projection import (
    WorldStateProjection,
    WorldView,
    WorldViewDiff,
    annotate_world_fact,
    diff_world_views,
)
from ...modules.project_manager import ProjectOrchestrator
from ...modules.project_manager.models import (
    A2AMessage,
    A2AMessageType,
    Project,
    ProjectStatus,
)
from ...modules.project_manager.workspace.models import ResourceGrant, with_grant
from ...platform.contracts.interfaces import (
    HumanApproval,
    KnowledgeGateway,
    Observer,
    ProjectPlanner,
)
from ...platform.contracts.mvp import JsonObject, KnowledgeItem, Observation, ProjectProposal
from ...platform.contracts.mvp import to_knowledge_item


@dataclass(frozen=True, slots=True)
class ClosedLoopRequest:
    """Application input for exactly one Observation-to-result pass."""

    goal: str
    resource_uris: tuple[str, ...] = ()
    project_context: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject an empty goal before it reaches a Planner."""

        if not self.goal.strip():
            raise ValueError("goal is required")


@dataclass(frozen=True, slots=True)
class PlanningContext:
    """The application-owned view passed to the Planner boundary.

    ``ProjectPlanner`` already accepts a ``KnowledgeItem``.  Keeping that
    frozen module contract, :meth:`as_planner_input` carries this DTO as its
    JSON content rather than adding a second Planner API.
    """

    goal: str
    observation: Observation
    relevant_knowledge: tuple[KnowledgeItem, ...]
    resource_uris: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        """Return a deterministic trace id for this planning pass."""

        return f"planning-{self.observation.id}"

    def as_planner_input(self) -> KnowledgeItem:
        """Convert the context to the Planner's existing public input type."""

        return KnowledgeItem(
            id=self.id,
            source="nexus_seed_planning_context",
            created_at=self.observation.observed_at,
            content={
                "goal": self.goal,
                "observation": {
                    "id": self.observation.id,
                    "content": self.observation.content,
                    "source": self.observation.source,
                    "metadata": dict(self.observation.metadata),
                },
                "relevant_knowledge": [
                    {
                        "id": item.id,
                        "content": item.content,
                        "source": item.source,
                        "metadata": dict(item.metadata),
                    }
                    for item in self.relevant_knowledge
                ],
                "resource_uris": list(self.resource_uris),
            },
            metadata={
                "goal": self.goal,
                "observation_id": self.observation.id,
                "knowledge_ids": [item.id for item in self.relevant_knowledge],
            },
        )


@dataclass(frozen=True, slots=True)
class A2AExecutionResult:
    """The structured outcome already persisted on the Project A2A channel."""

    project_id: str
    status: str
    summary: str
    payload: JsonObject = field(default_factory=dict)
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ClosedLoopRunReport:
    """Traceable result of one closed-loop pass."""

    state: str
    observation: Observation | None = None
    input_knowledge: KnowledgeItem | None = None
    planning_context: PlanningContext | None = None
    proposal: ProjectProposal | None = None
    approved: bool | None = None
    project: Project | None = None
    execution_result: A2AExecutionResult | None = None
    result_observation: Observation | None = None


@dataclass(frozen=True, slots=True)
class StableLoopRunReport:
    """Result of a bounded sequence of closed-loop passes."""

    status: str
    iterations: int
    stop_reason: str
    project_ids: tuple[str, ...] = ()
    last_world_diff: WorldViewDiff = field(default_factory=WorldViewDiff)
    runs: tuple[ClosedLoopRunReport, ...] = ()


class ClosedLoopMVPApplication:
    """Compose one closed loop without taking over any module's work."""

    def __init__(
        self,
        *,
        observer: Observer,
        knowledge: KnowledgeGateway,
        planner: ProjectPlanner,
        approval: HumanApproval,
        project_manager: ProjectOrchestrator,
        max_relevant_knowledge: int = 20,
        knowledge_ledger: KnowledgeLedger | None = None,
        project_settle_timeout: float = 1800.0,
        project_settle_interval: float = 2.0,
    ) -> None:
        if max_relevant_knowledge <= 0:
            raise ValueError("max_relevant_knowledge must be greater than zero")
        self.observer = observer
        self.knowledge = knowledge
        self.planner = planner
        self.approval = approval
        self.project_manager = project_manager
        self.max_relevant_knowledge = max_relevant_knowledge
        self.knowledge_ledger = knowledge_ledger or getattr(knowledge, "ledger", None)
        self.project_settle_timeout = project_settle_timeout
        self.project_settle_interval = project_settle_interval

    async def run_once(self, request: ClosedLoopRequest) -> ClosedLoopRunReport:
        """Observe, plan, delegate once through A2A, then record the outcome."""

        observations = self.observer.observe()
        if not observations:
            return ClosedLoopRunReport(state="NO_INPUT")
        if len(observations) != 1:
            raise ValueError("a closed-loop pass accepts exactly one observation")

        return await self._run_observation_once(request, observations[0])

    async def run_until_stable(
        self,
        request: ClosedLoopRequest,
        *,
        max_iterations: int = 3,
    ) -> StableLoopRunReport:
        """Run bounded closed-loop passes until the Knowledge World View settles."""

        if max_iterations <= 0:
            raise ValueError("max_iterations must be greater than zero")
        if self.knowledge_ledger is None:
            raise RuntimeError("run_until_stable requires a KnowledgeLedger")

        before = self._world_view()
        seen_states = {self._world_fingerprint(before)}
        runs: list[ClosedLoopRunReport] = []
        project_ids: list[str] = []
        last_world_diff = WorldViewDiff()
        next_observation: Observation | None = None

        for iteration in range(1, max_iterations + 1):
            report = (
                await self.run_once(request)
                if next_observation is None
                else await self._run_observation_once(request, next_observation)
            )
            runs.append(report)
            if report.project is not None:
                project_ids.append(report.project.id)

            stop_reason = self._immediate_stop_reason(report)
            if stop_reason is not None:
                return self._stable_report(
                    stop_reason,
                    runs,
                    project_ids,
                    last_world_diff,
                )

            self._apply_explicit_world_facts(report)
            after = self._world_view()
            last_world_diff = diff_world_views(before, after)
            if not last_world_diff:
                return self._stable_report(
                    "stable_world",
                    runs,
                    project_ids,
                    last_world_diff,
                )

            state_key = self._world_fingerprint(after)
            if state_key in seen_states:
                return self._stable_report(
                    "repeated_state",
                    runs,
                    project_ids,
                    last_world_diff,
                )
            seen_states.add(state_key)

            if iteration == max_iterations:
                return self._stable_report(
                    "max_iterations",
                    runs,
                    project_ids,
                    last_world_diff,
                )

            next_observation = self._world_diff_observation(report, last_world_diff, iteration)
            before = after

        raise AssertionError("bounded loop exited without a stop reason")

    async def _run_observation_once(
        self,
        request: ClosedLoopRequest,
        observation: Observation,
    ) -> ClosedLoopRunReport:
        """Run one pass for an already acquired or application-derived observation."""

        input_knowledge = to_knowledge_item(observation)
        self.knowledge.put(input_knowledge)
        planning_context = PlanningContext(
            goal=request.goal,
            observation=observation,
            relevant_knowledge=self._relevant_knowledge(request.goal, input_knowledge),
            resource_uris=request.resource_uris,
        )
        proposals = self.planner.propose(planning_context.as_planner_input())
        if not proposals:
            return ClosedLoopRunReport(
                state="NO_ACTION",
                observation=observation,
                input_knowledge=input_knowledge,
                planning_context=planning_context,
            )
        if len(proposals) != 1:
            raise ValueError("a one-shot closed loop requires zero or one proposal")

        proposal = proposals[0]
        approved = self.approval.approve(proposal)
        if not approved:
            return ClosedLoopRunReport(
                state="REJECTED",
                observation=observation,
                input_knowledge=input_knowledge,
                planning_context=planning_context,
                proposal=proposal,
                approved=False,
            )

        project = await self.project_manager.start_project(
            proposal.goal,
            context=self._project_context(request, planning_context, proposal),
        )
        if self.project_manager.is_working(project):
            settled = await self.project_manager.settle(
                project.id,
                timeout=self.project_settle_timeout,
                interval=self.project_settle_interval,
            )
            if settled is not None:
                project = settled
        message = self._result_message(project)
        execution_result = self._execution_result(project, message)
        result_observation = self._result_observation(
            observation, planning_context, proposal, execution_result
        )
        if result_observation is not None:
            self.knowledge.put(to_knowledge_item(result_observation))

        return ClosedLoopRunReport(
            state="COMPLETED" if project.status is ProjectStatus.COMPLETED else "EXECUTION_STOPPED",
            observation=observation,
            input_knowledge=input_knowledge,
            planning_context=planning_context,
            proposal=proposal,
            approved=True,
            project=project,
            execution_result=execution_result,
            result_observation=result_observation,
        )

    def _world_view(self) -> WorldView:
        """Read the current Knowledge-derived World View."""

        if self.knowledge_ledger is None:
            raise RuntimeError("a KnowledgeLedger is required for World View access")
        return WorldStateProjection(self.knowledge_ledger).view()

    def _apply_explicit_world_facts(self, report: ClosedLoopRunReport) -> None:
        """Project only valid, explicitly returned ``world_facts`` into Knowledge."""

        if (
            report.execution_result is None
            or report.result_observation is None
            or self.knowledge_ledger is None
        ):
            return
        raw_facts = report.execution_result.payload.get("world_facts")
        if not isinstance(raw_facts, list):
            return

        current_facts = dict(self._world_view().facts)
        for raw_fact in raw_facts:
            if not isinstance(raw_fact, dict):
                continue
            entity = raw_fact.get("entity")
            attribute = raw_fact.get("attribute")
            if (
                not isinstance(entity, str)
                or not entity.strip()
                or not isinstance(attribute, str)
                or not attribute.strip()
                or "value" not in raw_fact
            ):
                continue
            entity = entity.strip()
            attribute = attribute.strip()
            value = raw_fact["value"]
            key = (entity, attribute)
            current = current_facts.get(key)
            if current is not None and current.value == value:
                continue

            knowledge_id = (
                current.knowledge_id
                if current is not None
                else self._world_fact_knowledge_id(entity, attribute)
            )
            self.knowledge.put(
                KnowledgeItem(
                    id=knowledge_id,
                    source="project_result_world_fact",
                    created_at=report.result_observation.observed_at,
                    content={
                        "entity": entity,
                        "attribute": attribute,
                        "value": value,
                    },
                    metadata={
                        "project_id": report.execution_result.project_id,
                        "result_observation_id": report.result_observation.id,
                    },
                )
            )
            annotate_world_fact(
                self.knowledge_ledger,
                knowledge_id,
                entity=entity,
                attribute=attribute,
                value=value,
                created_by="nexus_seed_closed_loop",
                recorded_at=report.result_observation.observed_at,
            )
            current_facts = dict(self._world_view().facts)

    @staticmethod
    def _world_fact_knowledge_id(entity: str, attribute: str) -> str:
        identity = f"{entity}\0{attribute}".encode("utf-8")
        return f"world-fact-{hashlib.sha256(identity).hexdigest()[:24]}"

    @staticmethod
    def _world_fingerprint(view: WorldView) -> str:
        return json.dumps(
            view.snapshot(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _world_diff_observation(
        report: ClosedLoopRunReport,
        world_diff: WorldViewDiff,
        iteration: int,
    ) -> Observation:
        """Turn a meaningful World View change into the next planning input."""

        project_id = report.project.id if report.project is not None else "unknown"
        return Observation(
            id=f"world-diff-{iteration}-{project_id}",
            source="knowledge_world_diff",
            content={
                "world_changes": [
                    {
                        "entity": change.entity,
                        "attribute": change.attribute,
                        "old_value": change.old_value,
                        "new_value": change.new_value,
                        "change": change.change,
                    }
                    for change in world_diff.changes
                ]
            },
            metadata={
                "iteration": iteration + 1,
                "previous_project_id": project_id,
            },
        )

    @staticmethod
    def _immediate_stop_reason(report: ClosedLoopRunReport) -> str | None:
        if report.state == "NO_ACTION":
            return "no_action"
        if report.state == "NO_INPUT":
            return "no_input"
        if report.state == "REJECTED":
            return "rejected"
        if report.project is not None and report.project.status is ProjectStatus.BLOCKED:
            return "blocked"
        if report.state == "EXECUTION_STOPPED":
            return "execution_stopped"
        return None

    @staticmethod
    def _stable_report(
        stop_reason: str,
        runs: list[ClosedLoopRunReport],
        project_ids: list[str],
        last_world_diff: WorldViewDiff,
    ) -> StableLoopRunReport:
        if stop_reason in {"no_action", "stable_world"}:
            status = "stable"
        elif stop_reason == "blocked":
            status = "blocked"
        elif stop_reason == "max_iterations":
            status = "limit_reached"
        else:
            status = "stopped"
        return StableLoopRunReport(
            status=status,
            iterations=len(runs),
            stop_reason=stop_reason,
            project_ids=tuple(project_ids),
            last_world_diff=last_world_diff,
            runs=tuple(runs),
        )

    def _relevant_knowledge(
        self, goal: str, input_knowledge: KnowledgeItem
    ) -> tuple[KnowledgeItem, ...]:
        """Select bounded Knowledge context without exposing a backend to Planner."""

        selected = {input_knowledge.id: input_knowledge}
        observation_query = json.dumps(
            input_knowledge.content,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        for query in (goal, observation_query):
            for item in self.knowledge.search(query):
                selected.setdefault(item.id, item)
                if len(selected) >= self.max_relevant_knowledge:
                    return tuple(selected.values())
        return tuple(selected.values())

    @staticmethod
    def _project_context(
        request: ClosedLoopRequest,
        planning_context: PlanningContext,
        proposal: ProjectProposal,
    ) -> JsonObject:
        """Translate an approved Plan to ordinary Project context and grants."""

        context = dict(request.project_context)
        context["closed_loop"] = {
            "observation_id": planning_context.observation.id,
            "planning_context_id": planning_context.id,
            "proposal_id": proposal.id,
            "knowledge_ids": [item.id for item in planning_context.relevant_knowledge],
        }
        for uri in request.resource_uris:
            context = with_grant(
                context,
                ResourceGrant(uri=uri, reason="resource referenced by closed-loop input"),
            )
        return context

    def _result_message(self, project: Project) -> A2AMessage | None:
        """Read the persisted inbound A2A result, never an Agent's internals."""

        inbound = [
            message
            for direction, message in self.project_manager.gateway.history(project.id)
            if direction == "inbound"
        ]
        if project.status is ProjectStatus.COMPLETED:
            return next(
                (
                    message
                    for message in reversed(inbound)
                    if message.type is A2AMessageType.PROJECT_COMPLETED
                ),
                None,
            )
        return inbound[-1] if inbound else None

    @staticmethod
    def _execution_result(
        project: Project, message: A2AMessage | None
    ) -> A2AExecutionResult | None:
        """Expose a settled A2A outcome in an application-neutral record."""

        if message is None and project.status in {
            ProjectStatus.CREATED,
            ProjectStatus.ACTIVE,
        }:
            return None
        payload = dict(message.payload) if message else {}
        summary = str(payload.get("summary") or project.summary)
        return A2AExecutionResult(
            project_id=project.id,
            status=project.status.value.lower(),
            summary=summary,
            payload=payload,
            message_id=message.id if message else None,
        )

    @staticmethod
    def _result_observation(
        input_observation: Observation,
        planning_context: PlanningContext,
        proposal: ProjectProposal,
        result: A2AExecutionResult | None,
    ) -> Observation | None:
        """Make a Project result observable before returning it to Knowledge."""

        if result is None:
            return None
        return Observation(
            id=f"result-observation-{result.project_id}",
            source="project_result",
            content={
                "project_id": result.project_id,
                "status": result.status,
                "summary": result.summary,
                "result": dict(result.payload),
            },
            metadata={
                "input_observation_id": input_observation.id,
                "planning_context_id": planning_context.id,
                "proposal_id": proposal.id,
                "a2a_message_id": result.message_id,
            },
        )


__all__ = [
    "A2AExecutionResult",
    "ClosedLoopMVPApplication",
    "ClosedLoopRequest",
    "ClosedLoopRunReport",
    "PlanningContext",
    "StableLoopRunReport",
]
