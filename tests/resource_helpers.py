"""Shared scaffolding for the Phase 3E resource tests (not a test module)."""

from __future__ import annotations

from nexus_seed.adapters.file_watch import LocalFileAdapter
from nexus_seed.backends import LocalFileActionBackend
from nexus_seed.ingress.service import IngressService
from nexus_seed.processes.actions import bootstrap_actions
from nexus_seed.processes.resources import bootstrap_observer, bootstrap_resources
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence
from nexus_seed.resources.scope import ResourceScope

#: A document whose text is directly readable as world facts.
FACT_DOCUMENT = "# measurement report\nD1_CD.analysis_result=within spec\n"


def watched_tree(tmp_path, name: str = "watched"):
    """Create and return a watched directory."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def resource_runtime(runtime, watch_root, *, observer: bool = False):
    """Register the resource pipeline over ``watch_root`` and return the adapter."""
    scope = ResourceScope.read_only(watch_root)
    bootstrap_resources(runtime, scope=scope)
    if observer:
        bootstrap_observer(runtime)
    adapter = LocalFileAdapter(watch_root)
    runtime.register_adapter(adapter)
    return adapter


def full_stack(runtime, watch_root, action_root=None, *, observer: bool = False):
    """Resource pipeline + semantic + work + action, over one watched tree.

    Returns ``(adapter, action_backend)``.
    """
    adapter = resource_runtime(runtime, watch_root, observer=observer)
    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)
    bootstrap_actions(runtime)
    backend = None
    if action_root is not None:
        backend = LocalFileActionBackend(action_root)
        runtime.register_backend("local_file_action", backend)
        runtime.register_backend("local_file", backend)
    return adapter, backend


def ingress(runtime) -> IngressService:
    """The runtime's ingress service."""
    return runtime.ingress


def instances_named(runtime, name) -> list:
    """Return every instance of the definition ``name``, oldest first."""
    return [i for i in runtime.process_store.all_instances() if i.definition_name == name]


def only_resource(runtime):
    """Return the single Resource in the runtime (asserting there is one)."""
    resources = runtime.get_resources()
    assert len(resources) == 1, f"expected one resource, got {len(resources)}"
    return resources[0]


def write_file(root, path, content: str):
    """Write exact bytes into a watched tree (no newline translation)."""
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("utf-8"))
    return target


async def write_and_scan(adapter, path, content: str):
    """Write a file into the watched tree and run one poll+ingest cycle."""
    write_file(adapter.allowed_root, path, content)
    return await adapter.poll_and_ingest()
