"""Small read-only facade exposed to Process handlers as ``ctx.services``."""

from __future__ import annotations


class RuntimeServices:
    """Current cross-cutting reads; all writes remain ProcessResult effects."""

    def __init__(self, runtime) -> None:
        self._runtime = runtime

    def get_project_situation(self, project_id: str):
        """Compile one Project-centered situation projection."""
        from ..projects.projections import get_project_situation

        return get_project_situation(self._runtime, project_id)

    def get_project_summaries(self):
        """Compile compact summaries of every Project."""
        from ..projects.projections import get_project_summaries

        return get_project_summaries(self._runtime)

    def get_process_instance(self, instance_id):
        """Return one durable ProcessInstance."""
        return self._runtime.process_store.get_instance(instance_id)

    def get_definition(self, name: str, version: str):
        """Return one durable ProcessDefinition."""
        return self._runtime.process_store.get_definition(name, version)

    def get_resource_service(self):
        """Return the Resource read/index service."""
        return self._runtime.resource_service

    def get_extractors(self):
        """Return the deterministic extractor registry."""
        return self._runtime.extractors

    def get_representation(self, representation_id):
        """Return one ResourceRepresentation."""
        return self._runtime.resource_store.get_representation(representation_id)

    def get_ingress_receipt_for_event(self, event_id):
        """Return provenance for an externally ingested Event."""
        return self._runtime.ingress_receipt_store.for_event(event_id)

    def get_resource(self, resource_id):
        """Return one Resource."""
        return self._runtime.resource_store.get_resource(resource_id)

    def get_resource_by_uri(self, uri: str):
        """Return one Resource by stable URI."""
        return self._runtime.resource_store.get_resource_by_uri(uri)

    def get_current_resource_version(self, resource_id):
        """Return the latest ResourceVersion."""
        return self._runtime.resource_store.get_current_version(resource_id)

    def get_current_state(self, entity: str, attribute: str):
        """Return one current durable state entry."""
        return self._runtime.state_store.get_current(entity, attribute)

    def get_current_state_by_prefix(self, entity_prefix: str):
        """Return current state entries whose entity begins with a prefix."""
        return [
            entry
            for entry in self._runtime.state_store.all_current()
            if entry.entity.startswith(entity_prefix)
        ]
