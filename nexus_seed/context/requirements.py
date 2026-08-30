"""Explicit, deterministic slices compiled for one Process activation."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EntityAttributes:
    """Specific attributes of one state entity to include."""

    entity: str
    attributes: list[str] = field(default_factory=list)


@dataclass
class WorldStateReq:
    """Which durable state facts to include."""

    entities: list[str] = field(default_factory=list)
    entity_attributes: list[EntityAttributes] = field(default_factory=list)


@dataclass
class EventsReq:
    """Which recent or entity-related Events to include."""

    recent: int | None = None
    related_entities: list[str] = field(default_factory=list)


@dataclass
class ProcessTreeReq:
    """Which parent/child Process records to include."""

    parent: bool = False
    children: bool = False


@dataclass
class ContinuationReq:
    """Whether to include the active Continuation."""

    include: bool = False


@dataclass
class ResourcesReq:
    """Which Resource representations to compile into Context."""

    ids: list[str] = field(default_factory=list)
    uris: list[str] = field(default_factory=list)
    from_process_input: bool = True
    representations: list[str] = field(default_factory=lambda: ["text"])
    latest_only: bool = True
    max_items: int = 5
    max_bytes: int | None = None
    on_oversize: str = "truncate"


@dataclass
class ContextRequirements:
    """A ProcessDefinition's complete read-only Context declaration."""

    include_trigger_event: bool = True
    world_state: WorldStateReq | None = None
    events: EventsReq | None = None
    process_tree: ProcessTreeReq | None = None
    continuation: ContinuationReq | None = None
    resources: ResourcesReq | None = None

    def to_dict(self) -> dict:
        """Serialize to JSON-safe data for ProcessDefinition persistence."""
        result: dict = {"include_trigger_event": self.include_trigger_event}
        if self.world_state is not None:
            result["world_state"] = {
                "entities": list(self.world_state.entities),
                "entity_attributes": [
                    {"entity": item.entity, "attributes": list(item.attributes)}
                    for item in self.world_state.entity_attributes
                ],
            }
        if self.events is not None:
            result["events"] = {
                "recent": self.events.recent,
                "related_entities": list(self.events.related_entities),
            }
        if self.process_tree is not None:
            result["process_tree"] = {
                "parent": self.process_tree.parent,
                "children": self.process_tree.children,
            }
        if self.continuation is not None:
            result["continuation"] = {"include": self.continuation.include}
        if self.resources is not None:
            result["resources"] = {
                "ids": list(self.resources.ids),
                "uris": list(self.resources.uris),
                "from_process_input": self.resources.from_process_input,
                "representations": list(self.resources.representations),
                "latest_only": self.resources.latest_only,
                "max_items": self.resources.max_items,
                "max_bytes": self.resources.max_bytes,
                "on_oversize": self.resources.on_oversize,
            }
        return result

    @classmethod
    def from_dict(cls, data: dict | None) -> "ContextRequirements | None":
        """Load current fields while safely ignoring retired legacy fields."""
        if not data:
            return None
        world = data.get("world_state")
        events = data.get("events")
        tree = data.get("process_tree")
        continuation = data.get("continuation")
        resources = data.get("resources")
        return cls(
            include_trigger_event=data.get("include_trigger_event", True),
            world_state=(
                WorldStateReq(
                    entities=list(world.get("entities", [])),
                    entity_attributes=[
                        EntityAttributes(
                            entity=item["entity"],
                            attributes=list(item.get("attributes", [])),
                        )
                        for item in world.get("entity_attributes", [])
                        if isinstance(item, dict) and item.get("entity")
                    ],
                )
                if isinstance(world, dict)
                else None
            ),
            events=(
                EventsReq(
                    recent=events.get("recent"),
                    related_entities=list(events.get("related_entities", [])),
                )
                if isinstance(events, dict)
                else None
            ),
            process_tree=(
                ProcessTreeReq(
                    parent=tree.get("parent", False),
                    children=tree.get("children", False),
                )
                if isinstance(tree, dict)
                else None
            ),
            continuation=(
                ContinuationReq(include=continuation.get("include", False))
                if isinstance(continuation, dict)
                else None
            ),
            resources=(
                ResourcesReq(
                    ids=list(resources.get("ids", [])),
                    uris=list(resources.get("uris", [])),
                    from_process_input=resources.get("from_process_input", True),
                    representations=list(resources.get("representations", ["text"])),
                    latest_only=resources.get("latest_only", True),
                    max_items=resources.get("max_items", 5),
                    max_bytes=resources.get("max_bytes"),
                    on_oversize=resources.get("on_oversize", "truncate"),
                )
                if isinstance(resources, dict)
                else None
            ),
        )
