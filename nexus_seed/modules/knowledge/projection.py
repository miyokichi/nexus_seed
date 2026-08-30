"""WorldStateProjection — World Model as a read over the Knowledge Ledger.

Spec §10: ``World Model = HEAD``.  The existing ``world_state_*`` tables and
API (Phase 2B) are left completely alone; this module is a *second* way to
read a world view, built purely from the Knowledge Ledger, so the two can
coexist while the switch happens gradually (spec: "既存コードを大量に書き換
えるのではなくAdapter / Projectionで互換性を保つこと").

A Knowledge revision only becomes part of the World View once something has
read it as a fact — attaching a :data:`ANNOTATION_WORLD_FACT` Annotation
(``{"entity", "attribute", "value", "confidence"}``).  This is Structure on
Read in action: the raw Knowledge stays free text; a Typed View opts specific
objects into the entity/attribute projection without forcing that shape on
everything in the Ledger.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ...core.event import Event, utcnow
from .ledger import KnowledgeLedger
from .models import ANNOTATION_WORLD_FACT, KnowledgeSource

FactKey = tuple[str, str]


@dataclass
class WorldFact:
    """One projected ``entity.attribute`` fact, with its Knowledge lineage."""

    entity: str
    attribute: str
    value: Any
    knowledge_id: str
    revision: int
    confidence: float | None
    source: KnowledgeSource
    recorded_at: datetime


@dataclass
class WorldView:
    """A point-in-time projection of the Knowledge Ledger's world facts.

    ``conflicts`` holds every key where more than one Knowledge object
    currently claims a value — never silently collapsed to one (spec §12).
    ``facts`` still names one value per key (the most recently *recorded*
    claim) so ordinary callers get an answer; a caller that cares checks
    :meth:`is_conflicted` first.
    """

    facts: dict[FactKey, WorldFact] = field(default_factory=dict)
    conflicts: dict[FactKey, list[WorldFact]] = field(default_factory=dict)

    def get(self, entity: str, attribute: str, default: Any = None) -> Any:
        fact = self.facts.get((entity, attribute))
        return fact.value if fact is not None else default

    def is_conflicted(self, entity: str, attribute: str) -> bool:
        return (entity, attribute) in self.conflicts

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """``{entity: {attribute: value}}`` — the same shape as
        :meth:`nexus_seed.storage.state_store.StateStore.snapshot`."""
        out: dict[str, dict[str, Any]] = {}
        for (entity, attribute), fact in self.facts.items():
            out.setdefault(entity, {})[attribute] = fact.value
        return out


@dataclass
class FactChange:
    """One entity.attribute difference between two :class:`WorldView`s."""

    entity: str
    attribute: str
    old_value: Any
    new_value: Any
    change: str  # "added" | "changed" | "removed"


@dataclass
class WorldViewDiff:
    """The full difference between a ``before`` and ``after`` World View."""

    changes: list[FactChange] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.changes)

    def to_events(
        self,
        *,
        source: str = "knowledge_runtime",
        correlation_id: uuid.UUID | None = None,
    ) -> list[Event]:
        """Render each change as a ``state_changed`` :class:`Event`.

        The payload contains ``entity``, ``attribute``, ``old_value`` and
        ``new_value``. Callers may hand these events to the durable event loop
        with ``runtime.submit_event(event)``; Project creation still goes
        through the Orchestrator's public request boundary.
        """
        return [
            Event(
                type="state_changed",
                source=source,
                payload={
                    "entity": change.entity,
                    "attribute": change.attribute,
                    "old_value": change.old_value,
                    "new_value": change.new_value,
                    "change": change.change,
                },
                correlation_id=correlation_id,
            )
            for change in self.changes
        ]


def diff_world_views(before: WorldView, after: WorldView) -> WorldViewDiff:
    """The set of facts that differ between two projections."""
    changes: list[FactChange] = []
    keys = sorted(set(before.facts) | set(after.facts))
    for key in keys:
        b = before.facts.get(key)
        a = after.facts.get(key)
        if b is None and a is not None:
            changes.append(FactChange(*key, old_value=None, new_value=a.value, change="added"))
        elif b is not None and a is None:
            changes.append(FactChange(*key, old_value=b.value, new_value=None, change="removed"))
        elif b is not None and a is not None and b.value != a.value:
            changes.append(
                FactChange(*key, old_value=b.value, new_value=a.value, change="changed")
            )
    return WorldViewDiff(changes=changes)


class WorldStateProjection:
    """Builds a :class:`WorldView` from the Knowledge Ledger, at any time."""

    def __init__(self, ledger: KnowledgeLedger) -> None:
        self.ledger = ledger

    def view(
        self,
        *,
        as_of: datetime | None = None,
        valid_at: datetime | None = None,
    ) -> WorldView:
        """Project the current (or a past) World View.

        ``as_of`` replays **transaction time** — "what did NEXUS SEED
        believe at that point in its own history" — using only revisions
        recorded by then.  ``valid_at`` instead asks **valid time** — "what
        does NEXUS SEED currently believe was true in the world at that
        moment" — using each object's best-matching revision by
        ``valid_from``/``valid_to``.  Passing both is an error; passing
        neither returns the current view.
        """
        if as_of is not None and valid_at is not None:
            raise ValueError("pass at most one of as_of / valid_at")
        revisions = (
            self._heads_at_valid_time(valid_at)
            if valid_at is not None
            else self.ledger.all_heads(as_of=as_of)
        )
        return self._project(revisions)

    def _heads_at_valid_time(self, valid_at: datetime):
        candidate_ids = {
            rev.knowledge_id
            for rev in self.ledger.store.all_revisions()
            if rev.annotations_of(ANNOTATION_WORLD_FACT)
        }
        revisions = []
        for knowledge_id in candidate_ids:
            rev = self.ledger.valid_at(knowledge_id, valid_at)
            if rev is not None:
                revisions.append(rev)
        return revisions

    @staticmethod
    def _project(revisions) -> WorldView:
        facts: dict[FactKey, WorldFact] = {}
        conflicts: dict[FactKey, list[WorldFact]] = {}
        for rev in revisions:
            annotations = rev.annotations_of(ANNOTATION_WORLD_FACT)
            if not annotations:
                continue
            data = annotations[-1].value or {}
            entity, attribute = data.get("entity"), data.get("attribute")
            if not entity or not attribute:
                continue
            fact = WorldFact(
                entity=entity,
                attribute=attribute,
                value=data.get("value"),
                knowledge_id=rev.knowledge_id,
                revision=rev.revision,
                confidence=data.get("confidence"),
                source=rev.source,
                recorded_at=rev.recorded_at,
            )
            key = (entity, attribute)
            if key in facts and facts[key].knowledge_id != fact.knowledge_id:
                conflicts.setdefault(key, [facts[key]]).append(fact)
                if fact.recorded_at >= facts[key].recorded_at:
                    facts[key] = fact
            else:
                facts[key] = fact
        return WorldView(facts=facts, conflicts=conflicts)


def annotate_world_fact(
    ledger: KnowledgeLedger,
    knowledge_id: str,
    *,
    entity: str,
    attribute: str,
    value: Any,
    confidence: float | None = None,
    created_by: str | None = None,
    recorded_at: datetime | None = None,
):
    """Convenience: opt one Knowledge object into the World View projection."""
    from .models import Annotation

    payload = {"entity": entity, "attribute": attribute, "value": value}
    if confidence is not None:
        payload["confidence"] = confidence
    return ledger.annotate(
        knowledge_id,
        Annotation(kind=ANNOTATION_WORLD_FACT, value=payload, created_by=created_by, created_at=recorded_at or utcnow()),
        recorded_at=recorded_at,
    )
