"""Plan from retrieved Knowledge: what is required and not there yet.

The Planner reads only what the Knowledge Gateway handed it.  A relevant
Knowledge item whose content carries ``entities`` is *normalized* semantic
context — entities, their properties, relations and sources.  Which backend
produced it is not visible here and must not become visible: this planner asks
"is anything required still missing?", not "what did Semantica say?".

It is deliberately one rule, not a planning system.  When a required
deliverable has no status of its own yet, one Project is proposed to produce
it, and the Project is asked to report the fact it establishes back as
``world_facts`` so the loop can see the world change rather than infer it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ...platform.contracts.mvp import KnowledgeItem, ProjectProposal

#: The Canonical entity type carrying "something a person must be handed".
DELIVERABLE_TYPE = "Deliverable"

#: The property naming a deliverable's current state, and the value meaning
#: "not produced yet".  Both are Canonical property names, not backend fields.
STATUS_PROPERTY = "status"
MISSING_STATUS = "missing"
REQUIRED_PROPERTY = "required"


@dataclass(frozen=True, slots=True)
class RequiredDeliverable:
    """One required deliverable that retrieved Knowledge says is still missing."""

    id: str
    name: str
    artifact: str


class RequiredDeliverablePlanner:
    """Propose one Project for the first required deliverable still missing.

    ``propose`` returns an empty list — a correct and common answer — when
    nothing is required, when everything required already exists, or when the
    Observation being planned from already reports the deliverable as created.
    That last case is what stops the loop after replanning instead of proposing
    the same work forever.
    """

    def __init__(
        self,
        *,
        deliverable_type: str = DELIVERABLE_TYPE,
        artifact_suffix: str = ".txt",
    ) -> None:
        self.deliverable_type = deliverable_type
        self.artifact_suffix = artifact_suffix

    def propose(self, context: KnowledgeItem) -> list[ProjectProposal]:
        """Return zero or one proposal for the planning context handed in."""

        content = context.content
        if not isinstance(content, Mapping):
            return []
        settled = _settled_entities(content.get("observation"))
        for candidate in self._candidates(content.get("relevant_knowledge")):
            if candidate.id in settled:
                continue
            return [
                ProjectProposal(
                    title=f"Create {candidate.name}",
                    goal=(
                        f"Create {candidate.name} as {candidate.artifact} in the "
                        "granted workspace. Return world_facts with "
                        f"entity={candidate.id}, attribute={STATUS_PROPERTY}, "
                        "value=created."
                    ),
                    reason=(
                        f"Retrieved Knowledge marks required deliverable "
                        f"{candidate.id} as {MISSING_STATUS}."
                    ),
                    context={"deliverable_id": candidate.id},
                )
            ]
        return []

    def _candidates(self, relevant_knowledge: Any) -> Iterable[RequiredDeliverable]:
        """Yield required-but-missing deliverables, in retrieval order."""

        if not isinstance(relevant_knowledge, list):
            return
        seen: set[str] = set()
        for item in relevant_knowledge:
            content = item.get("content") if isinstance(item, Mapping) else None
            if not isinstance(content, Mapping):
                continue
            entities = content.get("entities")
            if not isinstance(entities, list):
                continue
            for entity in entities:
                candidate = self._deliverable(entity)
                if candidate is not None and candidate.id not in seen:
                    seen.add(candidate.id)
                    yield candidate

    def _deliverable(self, entity: Any) -> RequiredDeliverable | None:
        if not isinstance(entity, Mapping):
            return None
        if entity.get("type") != self.deliverable_type:
            return None
        properties = entity.get("properties")
        if not isinstance(properties, Mapping):
            return None
        if properties.get(REQUIRED_PROPERTY) is not True:
            return None
        if properties.get(STATUS_PROPERTY) != MISSING_STATUS:
            return None
        identifier = str(entity.get("id") or "").strip()
        if not identifier:
            return None
        return RequiredDeliverable(
            id=identifier,
            name=str(entity.get("name") or identifier),
            artifact=f"{identifier}{self.artifact_suffix}",
        )


def _settled_entities(observation: Any) -> set[str]:
    """Return entities this Observation already reports a new status for.

    The closed loop feeds a World View change back in as the next Observation.
    Reading it here is what makes replanning end: the deliverable that was
    missing is now reported created, so there is nothing left to propose.
    """

    if not isinstance(observation, Mapping):
        return set()
    content = observation.get("content")
    if not isinstance(content, Mapping):
        return set()
    changes = content.get("world_changes")
    if not isinstance(changes, list):
        return set()
    settled = set()
    for change in changes:
        if not isinstance(change, Mapping):
            continue
        entity = change.get("entity")
        if change.get("attribute") == STATUS_PROPERTY and isinstance(entity, str):
            settled.add(entity)
    return settled


__all__ = [
    "DELIVERABLE_TYPE",
    "MISSING_STATUS",
    "REQUIRED_PROPERTY",
    "RequiredDeliverable",
    "RequiredDeliverablePlanner",
    "STATUS_PROPERTY",
]
