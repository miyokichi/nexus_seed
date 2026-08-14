"""ContextRequirements — what a process declares it needs to see at run time.

Domain data, not a core primitive.  A :class:`ContextRequirements` is a
*structured, explicit* declaration (no query language yet) that the
:class:`~nexus_seed.context.compiler.ContextCompiler` reads to decide which
slices of persistent Memory to compile into a process's Context.

This module imports nothing from ``core`` so it can be referenced by
``core.process`` without an import cycle; it (de)serialises to plain dicts so a
ProcessDefinition can persist its requirements as JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EntityAttributes:
    """Specific attributes of one entity to include from world state."""

    entity: str
    attributes: list[str] = field(default_factory=list)


@dataclass
class WorldStateReq:
    """Which world-state facts to include."""

    entities: list[str] = field(default_factory=list)
    entity_attributes: list[EntityAttributes] = field(default_factory=list)
    #: Also include entities named by this process's WorkRequirement.
    include_work_entities: bool = False


@dataclass
class EventsReq:
    """Which events to include."""

    recent: int | None = None
    related_entities: list[str] = field(default_factory=list)


@dataclass
class ObservationsReq:
    """Which observations to include."""

    recent: int | None = None
    related_entities: list[str] = field(default_factory=list)


@dataclass
class StateDeltasReq:
    """Which state deltas to include."""

    recent: int | None = None
    related_entities: list[str] = field(default_factory=list)


@dataclass
class WorkReq:
    """Which work requirements to include."""

    current: bool = False
    related: bool = False


@dataclass
class ProcessTreeReq:
    """Which surrounding processes to include."""

    parent: bool = False
    children: bool = False


@dataclass
class ContinuationReq:
    """Whether to include the active continuation (e.g. on resume)."""

    include: bool = False


@dataclass
class ResourcesReq:
    """Which documents to compile into the context, and how (Phase 3E).

    Selection is **deterministic** (spec §28): explicit ids, explicit URIs, or
    ids named by this process's input / WorkRequirement metadata.  No semantic
    retrieval, no embeddings, no relevance ranking — those would make what a
    process saw unreproducible, which is exactly what the ContextSnapshot audit
    exists to prevent.

    Attributes:
        ids: Resource ids (as strings) to include.
        uris: Resource URIs to include.
        from_process_input: Also take ids/URIs from the instance input keys
            ``resource_ids`` / ``resource_uris``.
        from_work_metadata: Also take them from the WorkRequirement's metadata.
        representations: Which representation types to attach, in preference
            order — the first one present is used.
        latest_only: Use each Resource's current version.  ``False`` pins to the
            version recorded in the process input (``resource_versions``), so a
            process can deliberately keep reading what it started with.
        max_items: Cap on how many resources enter the context.
        max_bytes: Cap on one representation's serialized size.
        on_oversize: ``"truncate"`` (keep a prefix, flag it) or ``"exclude"``
            (drop the item).  Never summarisation — that would be interpretation
            hidden inside the compiler.
    """

    ids: list[str] = field(default_factory=list)
    uris: list[str] = field(default_factory=list)
    from_process_input: bool = True
    from_work_metadata: bool = False
    representations: list[str] = field(default_factory=lambda: ["text"])
    latest_only: bool = True
    max_items: int = 5
    max_bytes: int | None = None
    on_oversize: str = "truncate"


@dataclass
class ContextRequirements:
    """A process's full, explicit declaration of its context needs."""

    include_trigger_event: bool = True
    world_state: WorldStateReq | None = None
    events: EventsReq | None = None
    observations: ObservationsReq | None = None
    state_deltas: StateDeltasReq | None = None
    work: WorkReq | None = None
    process_tree: ProcessTreeReq | None = None
    continuation: ContinuationReq | None = None
    resources: ResourcesReq | None = None

    # --- serialization (for JSON persistence on ProcessDefinition) ---------

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict."""
        out: dict = {"include_trigger_event": self.include_trigger_event}
        if self.world_state is not None:
            out["world_state"] = {
                "entities": list(self.world_state.entities),
                "entity_attributes": [
                    {"entity": ea.entity, "attributes": list(ea.attributes)}
                    for ea in self.world_state.entity_attributes
                ],
                "include_work_entities": self.world_state.include_work_entities,
            }
        if self.events is not None:
            out["events"] = {
                "recent": self.events.recent,
                "related_entities": list(self.events.related_entities),
            }
        if self.observations is not None:
            out["observations"] = {
                "recent": self.observations.recent,
                "related_entities": list(self.observations.related_entities),
            }
        if self.state_deltas is not None:
            out["state_deltas"] = {
                "recent": self.state_deltas.recent,
                "related_entities": list(self.state_deltas.related_entities),
            }
        if self.work is not None:
            out["work"] = {"current": self.work.current, "related": self.work.related}
        if self.process_tree is not None:
            out["process_tree"] = {
                "parent": self.process_tree.parent,
                "children": self.process_tree.children,
            }
        if self.continuation is not None:
            out["continuation"] = {"include": self.continuation.include}
        if self.resources is not None:
            out["resources"] = {
                "ids": list(self.resources.ids),
                "uris": list(self.resources.uris),
                "from_process_input": self.resources.from_process_input,
                "from_work_metadata": self.resources.from_work_metadata,
                "representations": list(self.resources.representations),
                "latest_only": self.resources.latest_only,
                "max_items": self.resources.max_items,
                "max_bytes": self.resources.max_bytes,
                "on_oversize": self.resources.on_oversize,
            }
        return out

    @classmethod
    def from_dict(cls, data: dict | None) -> "ContextRequirements | None":
        """Reconstruct from a dict produced by :meth:`to_dict` (``None``-safe)."""
        if not data:
            return None
        ws = data.get("world_state")
        events = data.get("events")
        obs = data.get("observations")
        deltas = data.get("state_deltas")
        work = data.get("work")
        tree = data.get("process_tree")
        cont = data.get("continuation")
        res = data.get("resources")
        return cls(
            include_trigger_event=data.get("include_trigger_event", True),
            world_state=WorldStateReq(
                entities=list(ws.get("entities", [])),
                entity_attributes=[
                    EntityAttributes(entity=ea["entity"], attributes=list(ea.get("attributes", [])))
                    for ea in ws.get("entity_attributes", [])
                ],
                include_work_entities=ws.get("include_work_entities", False),
            )
            if ws is not None
            else None,
            events=EventsReq(
                recent=events.get("recent"),
                related_entities=list(events.get("related_entities", [])),
            )
            if events is not None
            else None,
            observations=ObservationsReq(
                recent=obs.get("recent"),
                related_entities=list(obs.get("related_entities", [])),
            )
            if obs is not None
            else None,
            state_deltas=StateDeltasReq(
                recent=deltas.get("recent"),
                related_entities=list(deltas.get("related_entities", [])),
            )
            if deltas is not None
            else None,
            work=WorkReq(current=work.get("current", False), related=work.get("related", False))
            if work is not None
            else None,
            process_tree=ProcessTreeReq(
                parent=tree.get("parent", False), children=tree.get("children", False)
            )
            if tree is not None
            else None,
            continuation=ContinuationReq(include=cont.get("include", False))
            if cont is not None
            else None,
            resources=ResourcesReq(
                ids=list(res.get("ids", [])),
                uris=list(res.get("uris", [])),
                from_process_input=res.get("from_process_input", True),
                from_work_metadata=res.get("from_work_metadata", False),
                representations=list(res.get("representations", ["text"])),
                latest_only=res.get("latest_only", True),
                max_items=res.get("max_items", 5),
                max_bytes=res.get("max_bytes"),
                on_oversize=res.get("on_oversize", "truncate"),
            )
            if res is not None
            else None,
        )
