"""ContextCompiler — build a ProcessContextView from Memory, by declaration.

Pure mechanism: it reads *what* a process needs from its declared
:class:`ContextRequirements` and knows *where* to fetch it from the persistent
stores.  It performs no relevance reasoning, no LLM, no domain judgement — only
deterministic selection and ordering, so the same inputs always compile the
same view (Invariant 12: the view is a regenerable temporary projection).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace

from ..core.continuation import Continuation
from ..core.event import Event
from ..core.process import ProcessDefinition, ProcessInstance, ProcessStatus
from .models import ContextMetadata, ProcessContextView, ResourceContextItem
from .requirements import ContextRequirements


class ContextCompiler:
    """Compiles a :class:`ProcessContextView` for one process activation."""

    def __init__(
        self,
        *,
        process_store,
        event_store,
        state_store,
        continuation_store,
        resource_store=None,
    ) -> None:
        self.process_store = process_store
        self.event_store = event_store
        self.state_store = state_store
        self.continuation_store = continuation_store
        self.resource_store = resource_store

    def compile(
        self,
        *,
        definition: ProcessDefinition | None,
        process_instance: ProcessInstance,
        trigger_event: Event | None = None,
        continuation: Continuation | None = None,
    ) -> ProcessContextView:
        """Compile the read-only view this activation should see."""
        reqs = definition.context_requirements if definition is not None else None

        include_trigger = reqs is None or reqs.include_trigger_event
        trigger = trigger_event if include_trigger else None

        if reqs is None:
            # Minimal context: process_instance + trigger_event only.
            return self._finalize(
                ProcessContextView(process_instance=process_instance, trigger_event=trigger)
            )

        world_state = self._compile_world_state(reqs, process_instance)
        events = self._compile_events(reqs)
        parent, children, child_results = self._compile_process_tree(reqs, process_instance)
        cont = self._compile_continuation(reqs, process_instance, continuation)
        resources = self._compile_resources(reqs, process_instance)

        return self._finalize(
            ProcessContextView(
                process_instance=process_instance,
                trigger_event=trigger,
                world_state=world_state,
                recent_events=events,
                parent_process=parent,
                child_processes=children,
                child_results=child_results,
                continuation=cont,
                resources=resources,
            )
        )

    # --- per-requirement compilation --------------------------------------

    def _compile_world_state(self, reqs, instance):
        req = reqs.world_state
        world_state: dict[str, dict] = {}
        if req is None:
            return world_state

        entities = set(req.entities)

        for entity in sorted(entities):
            entries = self.state_store.current_for_entity(entity)
            if entries:
                world_state[entity] = {e.attribute: e for e in entries}

        for ea in req.entity_attributes:
            bucket = world_state.setdefault(ea.entity, {})
            for attribute in ea.attributes:
                entry = self.state_store.get_current(ea.entity, attribute)
                if entry is not None:
                    bucket[attribute] = entry
        # Drop entities that turned out to have no facts.
        return {k: v for k, v in world_state.items() if v}

    def _compile_events(self, reqs) -> list[Event]:
        req = reqs.events
        if req is None:
            return []
        collected: dict = {}
        if req.recent:
            for event in self.event_store.recent(req.recent):
                collected[event.id] = event
        for entity in req.related_entities:
            for event in self.event_store.referencing_entity(entity):
                collected[event.id] = event
        return sorted(collected.values(), key=lambda e: (e.occurred_at, str(e.id)))

    def _compile_process_tree(self, reqs, instance):
        req = reqs.process_tree
        if req is None:
            return None, [], []
        parent = None
        children: list = []
        child_results: list = []
        if req.parent and instance.parent_process_id is not None:
            parent = self.process_store.get_instance(instance.parent_process_id)
        if req.children:
            children = self.process_store.children_of(instance.id)
            for child in children:
                if child.status is ProcessStatus.COMPLETED and "output" in child.local_state:
                    child_results.append(
                        {"id": str(child.id), "output": child.local_state["output"]}
                    )
        return parent, children, child_results

    def _compile_continuation(self, reqs, instance, continuation):
        req = reqs.continuation
        if req is None or not req.include:
            return None
        if continuation is not None:
            return continuation
        return self.continuation_store.for_instance(instance.id)

    # --- resources (Phase 3E) ---------------------------------------------

    def _compile_resources(self, reqs, instance):
        """Attach the declared documents, at the version they are at *now*.

        Deterministic selection only.  Because this runs on every activation,
        a process resumed after a file changed sees the new version — the same
        fresh-resume rule Phase 3A established for world state (Invariant 40).
        """
        req = reqs.resources
        if req is None or self.resource_store is None:
            return []

        resources = self._select_resources(req, instance)
        pinned = self._pinned_versions(req, instance)

        items: list[ResourceContextItem] = []
        for resource in resources[: max(req.max_items, 0)]:
            version = self._version_for(resource, req, pinned)
            if version is None:
                continue
            representation = self._representation_for(version, req)
            if representation is None:
                # The process asked for a rendering this version does not have.
                # Including it with empty content would invite a handler to
                # read "no text" as "the document is empty".
                continue
            content, truncated = self._bounded(representation, req)
            if content is None and not truncated:
                continue  # excluded as oversized
            items.append(
                ResourceContextItem(
                    resource=resource,
                    version=version,
                    representation=representation,
                    content=content,
                    truncated=truncated,
                )
            )
        return items

    def _select_resources(self, req, instance):
        """Resolve the declared ids/URIs to Resources, in a stable order."""
        ids: list[str] = list(req.ids)
        uris: list[str] = list(req.uris)

        if req.from_process_input:
            ids.extend(_as_str_list(instance.input.get("resource_ids")))
            uris.extend(_as_str_list(instance.input.get("resource_uris")))

        resolved: list = []
        seen: set = set()
        for raw in ids:
            resource = self._resource_by_id(raw)
            if resource is not None and resource.id not in seen:
                resolved.append(resource)
                seen.add(resource.id)
        for uri in uris:
            resource = self.resource_store.get_resource_by_uri(uri)
            if resource is not None and resource.id not in seen:
                resolved.append(resource)
                seen.add(resource.id)
        return resolved

    def _resource_by_id(self, raw):
        try:
            return self.resource_store.get_resource(uuid.UUID(str(raw)))
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _pinned_versions(req, instance) -> dict:
        """``{resource_id: version_id}`` a process explicitly pinned itself to."""
        if req.latest_only:
            return {}
        raw = instance.input.get("resource_versions")
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def _version_for(self, resource, req, pinned):
        pinned_id = pinned.get(str(resource.id))
        if pinned_id:
            try:
                return self.resource_store.get_version(uuid.UUID(pinned_id))
            except ValueError:
                return None
        return self.resource_store.get_current_version(resource.id)

    def _representation_for(self, version, req):
        """Pick the first declared representation type this version has."""
        for representation_type in req.representations:
            found = self.resource_store.find_representation(version.id, representation_type)
            if found is not None:
                return found
        return None

    @staticmethod
    def _bounded(representation, req):
        """Apply ``max_bytes`` to a representation's content (spec §77-§78).

        Truncation is a blunt prefix cut, deliberately: summarising here would
        be interpretation hidden inside a mechanism, and the process could no
        longer tell what it had actually been given.
        """
        if representation is None:
            return None, False
        content = representation.content
        if req.max_bytes is None:
            return content, False

        serialized = content if isinstance(content, str) else json.dumps(content, default=str)
        if len(serialized.encode("utf-8")) <= req.max_bytes:
            return content, False
        if req.on_oversize == "exclude":
            return None, False
        return serialized.encode("utf-8")[: req.max_bytes].decode("utf-8", "ignore"), True

    # --- metadata ----------------------------------------------------------

    @staticmethod
    def _finalize(view: ProcessContextView) -> ProcessContextView:
        counts = {
            "world_state": sum(len(attrs) for attrs in view.world_state.values()),
            "recent_events": len(view.recent_events),
            "child_processes": len(view.child_processes),
            "resources": len(view.resources),
        }
        approx = len(json.dumps(view.to_snapshot_dict(), default=str))
        approx += sum(
            len(json.dumps(item.content, default=str)) for item in view.resources
        )
        return replace(
            view, metadata=ContextMetadata(item_counts=counts, approx_size_bytes=approx)
        )


def _as_str_list(value) -> list[str]:
    """Coerce a possibly-missing input field to a list of strings."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []
