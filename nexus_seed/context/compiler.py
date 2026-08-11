"""ContextCompiler — build a ProcessContextView from Memory, by declaration.

Pure mechanism: it reads *what* a process needs from its declared
:class:`ContextRequirements` and knows *where* to fetch it from the persistent
stores.  It performs no relevance reasoning, no LLM, no domain judgement — only
deterministic selection and ordering, so the same inputs always compile the
same view (Invariant 12: the view is a regenerable temporary projection).
"""

from __future__ import annotations

import json
from dataclasses import replace

from ..core.continuation import Continuation
from ..core.event import Event
from ..core.process import ProcessDefinition, ProcessInstance, ProcessStatus
from .models import ContextMetadata, ProcessContextView
from .requirements import ContextRequirements


class ContextCompiler:
    """Compiles a :class:`ProcessContextView` for one process activation."""

    def __init__(
        self,
        *,
        process_store,
        event_store,
        state_store,
        observation_store,
        state_delta_store,
        work_requirement_store,
        continuation_store,
    ) -> None:
        self.process_store = process_store
        self.event_store = event_store
        self.state_store = state_store
        self.observation_store = observation_store
        self.state_delta_store = state_delta_store
        self.work_requirement_store = work_requirement_store
        self.continuation_store = continuation_store

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
        observations = self._compile_observations(reqs)
        state_deltas = self._compile_state_deltas(reqs)
        work_requirements = self._compile_work(reqs, process_instance, world_state)
        parent, children, child_results = self._compile_process_tree(reqs, process_instance)
        cont = self._compile_continuation(reqs, process_instance, continuation)

        return self._finalize(
            ProcessContextView(
                process_instance=process_instance,
                trigger_event=trigger,
                world_state=world_state,
                recent_events=events,
                observations=observations,
                state_deltas=state_deltas,
                work_requirements=work_requirements,
                parent_process=parent,
                child_processes=children,
                child_results=child_results,
                continuation=cont,
            )
        )

    # --- per-requirement compilation --------------------------------------

    def _compile_world_state(self, reqs, instance):
        req = reqs.world_state
        world_state: dict[str, dict] = {}
        if req is None:
            return world_state

        entities = set(req.entities)
        if req.include_work_entities and instance.work_requirement_id is not None:
            requirement = self.work_requirement_store.get(instance.work_requirement_id)
            if requirement is not None:
                entities.update(requirement.related_entities)

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

    def _compile_observations(self, reqs):
        req = reqs.observations
        if req is None:
            return []
        collected: dict = {}
        if req.recent:
            for obs in self.observation_store.recent(req.recent):
                collected[obs.id] = obs
        for entity in req.related_entities:
            for obs in self.observation_store.by_subject(entity):
                collected[obs.id] = obs
        return sorted(collected.values(), key=lambda o: (o.created_at, str(o.id)))

    def _compile_state_deltas(self, reqs):
        req = reqs.state_deltas
        if req is None:
            return []
        collected: dict = {}
        if req.recent:
            for delta in self.state_delta_store.recent(req.recent):
                collected[delta.id] = delta
        for entity in req.related_entities:
            for delta in self.state_delta_store.by_entity(entity):
                collected[delta.id] = delta
        return sorted(collected.values(), key=lambda d: (d.created_at, str(d.id)))

    def _compile_work(self, reqs, instance, world_state):
        req = reqs.work
        if req is None:
            return []
        seen: set = set()
        result = []
        current = None
        if req.current and instance.work_requirement_id is not None:
            current = self.work_requirement_store.get(instance.work_requirement_id)
            if current is not None:
                result.append(current)
                seen.add(current.id)
        if req.related:
            entities = set(current.related_entities) if current is not None else set()
            entities.update(world_state.keys())
            for requirement in self.work_requirement_store.all():
                if requirement.id in seen:
                    continue
                if set(requirement.related_entities) & entities:
                    result.append(requirement)
                    seen.add(requirement.id)
        return result

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

    # --- metadata ----------------------------------------------------------

    @staticmethod
    def _finalize(view: ProcessContextView) -> ProcessContextView:
        counts = {
            "world_state": sum(len(attrs) for attrs in view.world_state.values()),
            "recent_events": len(view.recent_events),
            "observations": len(view.observations),
            "state_deltas": len(view.state_deltas),
            "work_requirements": len(view.work_requirements),
            "child_processes": len(view.child_processes),
        }
        approx = len(json.dumps(view.to_snapshot_dict(), default=str))
        return replace(
            view, metadata=ContextMetadata(item_counts=counts, approx_size_bytes=approx)
        )
