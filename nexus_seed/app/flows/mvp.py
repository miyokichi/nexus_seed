"""Thin application service that connects the replaceable MVP modules."""

from __future__ import annotations

from ...platform.contracts.interfaces import (
    HumanApproval,
    KnowledgeGateway,
    Observer,
    ProjectExecutor,
    ProjectManager,
    ProjectPlanner,
)
from ...platform.contracts.mvp import (
    MVPRunReport,
    ProjectExecutionRequest,
    ProjectResultStatus,
    to_knowledge_item,
)


class MVPApplicationRuntime:
    """Connect MVP modules in order without owning their decision logic."""

    def __init__(
        self,
        *,
        observer: Observer,
        knowledge: KnowledgeGateway,
        planner: ProjectPlanner,
        approval: HumanApproval,
        project_manager: ProjectManager,
        executor: ProjectExecutor,
        constraints=None,
        workspace: str | None = None,
    ) -> None:
        self.observer = observer
        self.knowledge = knowledge
        self.planner = planner
        self.approval = approval
        self.project_manager = project_manager
        self.executor = executor
        self.constraints = constraints
        self.workspace = workspace

    def run(self) -> MVPRunReport:
        """Run Observer through result persistence once, in that order."""

        observations = self.observer.observe()
        proposals = []
        projects = []
        results = []
        rejected = []

        for observation in observations:
            knowledge_item = to_knowledge_item(observation)
            self.knowledge.put(knowledge_item)
            for proposal in self.planner.propose(knowledge_item):
                proposals.append(proposal)
                if not self.approval.approve(proposal):
                    rejected.append(proposal.id)
                    continue

                project = self.project_manager.create(proposal)
                project = self.project_manager.mark_running(project.id)
                result = self.executor.execute(
                    ProjectExecutionRequest(
                        project_id=project.id,
                        goal=project.goal,
                        context=project.context,
                        constraints=self.constraints,
                        workspace=self.workspace,
                    )
                )
                if result.project_id != project.id:
                    raise ValueError("executor returned a result for another project")
                if result.status == ProjectResultStatus.COMPLETED:
                    project = self.project_manager.mark_completed(project.id, result)
                else:
                    project = self.project_manager.mark_failed(
                        project.id,
                        result.error or result.summary,
                    )
                self.knowledge.put(to_knowledge_item(result))
                projects.append(project)
                results.append(result)

        return MVPRunReport(
            observations=tuple(observations),
            proposals=tuple(proposals),
            projects=tuple(projects),
            results=tuple(results),
            rejected_proposal_ids=tuple(rejected),
        )


__all__ = ["MVPApplicationRuntime"]
