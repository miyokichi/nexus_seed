"""One-shot MVP composition across the independently owned modules.

The flow owns only wiring.  Observer, Knowledge, Planner, Approval, Project
Manager, and the A2A Agent Runtime retain their existing responsibilities and
contracts.  In particular, no Agent implementation is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
    ) -> None:
        if max_relevant_knowledge <= 0:
            raise ValueError("max_relevant_knowledge must be greater than zero")
        self.observer = observer
        self.knowledge = knowledge
        self.planner = planner
        self.approval = approval
        self.project_manager = project_manager
        self.max_relevant_knowledge = max_relevant_knowledge

    async def run_once(self, request: ClosedLoopRequest) -> ClosedLoopRunReport:
        """Observe, plan, delegate once through A2A, then record the outcome."""

        observations = self.observer.observe()
        if not observations:
            return ClosedLoopRunReport(state="NO_INPUT")
        if len(observations) != 1:
            raise ValueError("a closed-loop pass accepts exactly one observation")

        observation = observations[0]
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

    def _relevant_knowledge(
        self, goal: str, input_knowledge: KnowledgeItem
    ) -> tuple[KnowledgeItem, ...]:
        """Select bounded lexical context without giving the Planner a Ledger."""

        selected = {input_knowledge.id: input_knowledge}
        for item in self.knowledge.search(goal):
            selected.setdefault(item.id, item)
            if len(selected) >= self.max_relevant_knowledge:
                break
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
]
