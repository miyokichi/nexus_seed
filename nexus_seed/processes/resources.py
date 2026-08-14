"""Resource processes: index, extract, interpret — and the observer loop.

Four ordinary Processes, no new Runtime feature (Invariant 38):

    file_created/modified   -> resource_indexer    -> Resource + Version
      resource_version_created -> extract_resource -> Representation
        representation_created -> interpret_resource -> Observation + StateDelta

    start_watch_files       -> watch_files         -> poll -> ingress
                                                   -> suspend_on_timer -> repeat

The last one is the point of the phase as much as the first three: a system that
watches the world continuously does **not** need a daemon abstraction.  A
Process that suspends on a timer, resumes, and re-suspends *is* a long-lived
service, and it inherits crash recovery, restart safety and auditability from
the machinery that was already there (Invariant 41).
"""

from __future__ import annotations

import logging
import uuid

from ..context.requirements import ContextRequirements
from ..core.process import (
    ProcessContext,
    ProcessDefinition,
    ProcessResult,
)
from ..resources.extractors import (
    ExtractionError,
    default_registry,
    resource_type_for,
)
from ..resources.models import ResourceRepresentation
from ..resources.scope import ScopeViolation
from ..resources.service import ResourceService, file_uri
from ..world.observation import Observation
from ..world.state_delta import StateDelta

logger = logging.getLogger("nexus_seed.process.resources")

#: Representation types the extractor process produces by default.
DEFAULT_REPRESENTATIONS = ("text",)


def _uuid(value) -> uuid.UUID | None:
    return uuid.UUID(str(value)) if value else None


# --- process definitions ---------------------------------------------------

RESOURCE_INDEXER = ProcessDefinition(
    name="resource_indexer",
    version="1",
    handler="resource_indexer",
    trigger_event_types=("file_created", "file_modified"),
    metadata={"role": "resource_indexer"},
    context_requirements=ContextRequirements(include_trigger_event=True),
)

EXTRACT_RESOURCE = ProcessDefinition(
    name="extract_resource",
    version="1",
    handler="extract_resource",
    trigger_event_types=("resource_version_created",),
    max_retries=1,
    metadata={"role": "extractor"},
    context_requirements=ContextRequirements(include_trigger_event=True),
)

INTERPRET_RESOURCE = ProcessDefinition(
    name="interpret_resource",
    version="1",
    handler="interpret_resource",
    trigger_event_types=("representation_created",),
    metadata={"role": "interpreter"},
    context_requirements=ContextRequirements(include_trigger_event=True),
)

WATCH_FILES = ProcessDefinition(
    name="watch_files",
    version="1",
    handler="watch_files",
    trigger_event_types=("start_watch_files",),
    metadata={"role": "observer"},
    context_requirements=ContextRequirements(include_trigger_event=True),
)


# --- indexing --------------------------------------------------------------


async def resource_indexer(ctx: ProcessContext) -> ProcessResult:
    """Turn a file event into a Resource + ResourceVersion.

    The adapter said "these bytes changed"; this process decides what that
    means for the *catalogue* — a new document, or a new version of one we
    already track.  Keeping those two jobs in different layers is why the
    adapter never touches the resource tables (spec §10).
    """
    assert ctx.event is not None
    service = _resource_service(ctx)
    if service is None:
        return ctx.fail("no resource service available")

    payload = ctx.event.payload
    path = payload.get("path")
    if not path:
        return ctx.fail("file event has no path")

    try:
        uri = file_uri(service.scope, path) if service.scope else f"file:///{path}"
    except ScopeViolation as exc:
        # Outside the readable roots: refuse to catalogue it at all (spec §65).
        logger.warning("refusing to index out-of-scope path %s: %s", path, exc)
        return ctx.complete(output={"indexed": False, "reason": str(exc)})

    receipt = None
    if ctx.services is not None and hasattr(ctx.services, "get_ingress_receipt_for_event"):
        receipt = ctx.services.get_ingress_receipt_for_event(ctx.event.id)

    result = service.index_observation(
        uri=uri,
        locator=path,
        observed_hash=payload.get("content_hash"),
        size_bytes=payload.get("size"),
        resource_type=resource_type_for(path),
        source_adapter_id=ctx.event.source,
        source_identity=receipt.source_event_key if receipt else None,
        source_event_id=ctx.event.id,
        ingress_receipt_id=receipt.id if receipt else None,
        metadata={"change_type": payload.get("change_type")},
    )

    if not result.created_version:
        # Same bytes as a version we already hold: not a change (spec §11).
        logger.info("resource %s unchanged (%s)", uri, result.reason)
        return ctx.complete(
            output={
                "indexed": False,
                "reason": result.reason,
                "resource_id": str(result.resource.id) if result.resource else None,
            }
        )

    ctx.add_resource(result.resource)
    ctx.add_resource_version(result.version)
    created = ctx.new_event(
        "resource_version_created",
        {
            "resource_id": str(result.resource.id),
            "resource_version_id": str(result.version.id),
            "uri": result.resource.uri,
            "resource_type": result.resource.resource_type,
            "version": result.version.version,
            "content_hash": result.version.content_hash,
            "locator": result.version.locator,
        },
    )
    logger.info(
        "resource %s -> v%d (%s)", uri, result.version.version, result.version.content_hash
    )
    return ctx.complete(
        output={
            "indexed": True,
            "resource_id": str(result.resource.id),
            "resource_version_id": str(result.version.id),
            "version": result.version.version,
        },
        emitted_events=[created],
    )


# --- extraction ------------------------------------------------------------


async def extract_resource(ctx: ProcessContext) -> ProcessResult:
    """Render a ResourceVersion into the declared Representations.

    Reads the content once, then asks the registry for an extractor per
    requested representation type.  An unsupported combination is not an error —
    not every document has every rendering (spec §13).
    """
    assert ctx.event is not None
    service = _resource_service(ctx)
    if service is None:
        return ctx.fail("no resource service available")

    payload = ctx.event.payload
    version_id = _uuid(payload.get("resource_version_id"))
    resource_type = payload.get("resource_type") or "unknown"
    locator = payload.get("locator") or ""

    registry = _extractor_registry(ctx)
    # Default: every rendering the registry can produce for this kind of
    # document.  A CSV genuinely has both a text and a structure form, and
    # deciding later which one a process wants is the Context compiler's job.
    requested = ctx.instance.input.get("representations") or registry.representation_types_for(
        resource_type
    )

    try:
        data = service.read_bytes(locator)
    except (OSError, ScopeViolation) as exc:
        logger.warning("cannot read %s for extraction: %s", locator, exc)
        return ctx.complete(
            output={"extracted": False, "reason": str(exc)},
            emitted_events=[
                ctx.new_event(
                    "representation_failed",
                    {
                        "resource_version_id": str(version_id) if version_id else None,
                        "error": str(exc),
                    },
                )
            ],
        )

    emitted = []
    produced = []
    for representation_type in requested:
        extractor = registry.find(representation_type, resource_type)
        if extractor is None:
            continue
        try:
            content = extractor.extract(data)
        except ExtractionError as exc:
            logger.warning("extractor %s failed: %s", extractor.name, exc)
            emitted.append(
                ctx.new_event(
                    "representation_failed",
                    {
                        "resource_version_id": str(version_id),
                        "representation_type": representation_type,
                        "extractor": extractor.name,
                        "error": str(exc),
                    },
                )
            )
            continue

        representation = ResourceRepresentation(
            resource_version_id=version_id,
            representation_type=extractor.representation_type,
            content=content,
            extractor_name=extractor.name,
            extractor_version=extractor.version,
            metadata={"resource_type": resource_type},
        )
        ctx.add_representation(representation)
        produced.append(representation)
        emitted.append(
            ctx.new_event(
                "representation_created",
                {
                    "resource_id": payload.get("resource_id"),
                    "resource_version_id": str(version_id),
                    "representation_id": str(representation.id),
                    "representation_type": representation.representation_type,
                    "extractor": extractor.name,
                    "uri": payload.get("uri"),
                },
            )
        )

    return ctx.complete(
        output={
            "extracted": bool(produced),
            "representation_ids": [str(r.id) for r in produced],
        },
        emitted_events=emitted,
    )


# --- interpretation --------------------------------------------------------

#: The trivially explicit format the demo interpreter understands.
FACT_SEPARATOR = "="


async def interpret_resource(ctx: ProcessContext) -> ProcessResult:
    """Read ``entity.attribute=value`` lines out of a text Representation.

    A deterministic demonstration that a document can reach World State
    *through the existing pipeline* (spec §37): it produces an Observation and a
    StateDelta and emits ``state_delta_created``, exactly like every other
    interpreter.  Nothing about documents gets a private write path.
    """
    assert ctx.event is not None
    payload = ctx.event.payload
    if payload.get("representation_type") != "text":
        return ctx.complete(output={"interpreted": 0, "reason": "not a text representation"})

    representation_id = _uuid(payload.get("representation_id"))
    representation = (
        ctx.services.get_representation(representation_id)
        if ctx.services is not None and hasattr(ctx.services, "get_representation")
        else None
    )
    if representation is None:
        return ctx.fail(f"representation {representation_id} not found")

    facts = parse_facts(representation.content)
    if not facts:
        return ctx.complete(output={"interpreted": 0})

    observation = Observation(
        subject=payload.get("uri") or "resource",
        predicate="facts_extracted",
        extracted={f"{e}.{a}": v for e, a, v in facts},
        source_event_id=ctx.event.id,
        created_by_process_id=ctx.instance.id,
        confidence=1.0,
    )
    ctx.add_observation(observation)

    emitted = []
    for entity, attribute, value in facts:
        current = (
            ctx.services.get_current_state(entity, attribute) if ctx.services else None
        )
        old_value = current.value if current is not None else None
        if old_value == value:
            continue  # the document agrees with what we already believe
        delta = StateDelta(
            entity=entity,
            attribute=attribute,
            old_value=old_value,
            new_value=value,
            source_event_id=ctx.event.id,
            observation_id=observation.id,
            created_by_process_id=ctx.instance.id,
            confidence=1.0,
            reason=f"read from {payload.get('uri')}",
        )
        ctx.add_state_delta(delta)
        emitted.append(
            ctx.new_event(
                "state_delta_created",
                {
                    "entity": entity,
                    "attribute": attribute,
                    "old_value": old_value,
                    "new_value": value,
                    "source_event_id": str(ctx.event.id),
                    "observation_id": str(observation.id),
                    "state_delta_id": str(delta.id),
                    "confidence": 1.0,
                    "resource_version_id": payload.get("resource_version_id"),
                },
            )
        )

    return ctx.complete(
        output={"interpreted": len(emitted), "observation_id": str(observation.id)},
        emitted_events=emitted,
    )


def parse_facts(content) -> list[tuple[str, str, object]]:
    """Parse ``entity.attribute=value`` lines into typed triples."""
    if not isinstance(content, str):
        return []
    facts: list[tuple[str, str, object]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or FACT_SEPARATOR not in line:
            continue
        key, _, raw = line.partition(FACT_SEPARATOR)
        entity, _, attribute = key.strip().partition(".")
        if not entity or not attribute:
            continue
        facts.append((entity, attribute, _coerce(raw.strip())))
    return facts


def _coerce(raw: str):
    """Turn a text value into int/float/bool where unambiguous."""
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    for caster in (int, float):
        try:
            return caster(raw)
        except ValueError:
            continue
    return raw


# --- the long-lived observer ----------------------------------------------

DEFAULT_POLL_INTERVAL = 60.0


async def watch_files(ctx: ProcessContext) -> ProcessResult:
    """Poll an adapter, ingest what it found, then sleep until the next tick.

    This is what a "resident service" is in NEXUS SEED: no daemon, no thread, no
    supervisor.  Between ticks the process is a row in SQLite and a
    Continuation, so it survives a restart for free, and every cycle it ran is
    in the ordinary process history (Invariant 41).

    The observation position is *not* kept here — it stays in the adapter's
    Checkpoint, which is the record of what the world showed us; the process's
    local state only counts cycles.
    """
    state = dict(ctx.saved_process_state or {})
    if not state:
        state = dict(ctx.instance.input)
        state.update(ctx.event.payload if ctx.event else {})

    adapter_id = state.get("adapter_id", "local_file")
    interval = float(state.get("poll_interval", DEFAULT_POLL_INTERVAL))
    max_cycles = state.get("max_cycles")
    poll_count = int(state.get("poll_count", 0))

    if ctx.event is not None and ctx.event.type == "stop_watch_files":
        logger.info("watch_files(%s) stopping after %d polls", adapter_id, poll_count)
        return ctx.complete(output={"stopped": True, "poll_count": poll_count})

    if ctx.adapters is None or ctx.ingress is None:
        return ctx.fail("watch_files needs an adapter registry and ingress service")

    try:
        adapter = ctx.adapters.get(adapter_id)
    except KeyError as exc:
        return ctx.fail(str(exc))

    ingested, failure = await _poll_once(ctx, adapter)
    poll_count += 1

    emitted = []
    if failure is not None:
        # A failed poll is reported, not retried in a loop (spec §45-§46): the
        # next tick is the retry, and the world will still be there.
        emitted.append(
            ctx.new_event(
                "observer_poll_failed",
                {"adapter_id": adapter_id, "error": failure, "poll_count": poll_count},
            )
        )

    state.update(
        {
            "adapter_id": adapter_id,
            "poll_interval": interval,
            "max_cycles": max_cycles,
            "poll_count": poll_count,
            "ingested_total": int(state.get("ingested_total", 0)) + len(ingested),
        }
    )
    logger.info(
        "watch_files(%s) poll %d ingested %d event(s)",
        adapter_id,
        poll_count,
        len(ingested),
    )

    if max_cycles is not None and poll_count >= int(max_cycles):
        return ctx.complete(
            output={"stopped": True, "poll_count": poll_count}, emitted_events=emitted
        )

    return ctx.suspend_on_timer(
        resume_point="poll",
        delay=interval,
        saved_process_state=state,
        also_waiting_for=[{"event_type": "stop_watch_files", "adapter_id": adapter_id}],
        emitted_events=emitted,
    )


async def _poll_once(ctx: ProcessContext, adapter) -> tuple[list, str | None]:
    """Poll and ingest, returning the ingested events and any failure message.

    Ingestion uses ``deliver=False`` and the observer does nothing further with
    the events.  Since Phase 3F it does not have to: the ingress boundary
    committed each event together with its *delivery obligation*, so the
    dispatcher will route them whether this activation commits, fails, or the
    runtime dies here (Invariant 47).  Handing them back for routing was the
    old gap; not needing to is the fix.
    """
    try:
        results = await adapter.poll_and_ingest(ctx.ingress, deliver=False)
    except Exception as exc:  # noqa: BLE001 - a bad source must not kill the observer
        logger.exception("adapter %s poll failed", getattr(adapter, "adapter_id", "?"))
        return [], str(exc)

    return [r.event for r in results if r.accepted and r.event is not None], None


# --- wiring ----------------------------------------------------------------


def _resource_service(ctx: ProcessContext) -> ResourceService | None:
    """The runtime's ResourceService, reached read-only through services."""
    if ctx.services is None or not hasattr(ctx.services, "get_resource_service"):
        return None
    return ctx.services.get_resource_service()


def _extractor_registry(ctx: ProcessContext):
    """The extractor registry, from the runtime if present, else the default."""
    registry = None
    if ctx.services is not None and hasattr(ctx.services, "get_extractors"):
        registry = ctx.services.get_extractors()
    return registry if registry is not None else default_registry()


def bootstrap_resources(runtime, *, scope=None) -> None:
    """Register the resource pipeline and (optionally) its read scope."""
    if scope is not None:
        runtime.set_resource_scope(scope)
    runtime.register_process(RESOURCE_INDEXER, resource_indexer)
    runtime.register_process(EXTRACT_RESOURCE, extract_resource)
    runtime.register_process(INTERPRET_RESOURCE, interpret_resource)


def bootstrap_observer(runtime) -> None:
    """Register the long-lived ``watch_files`` observer process."""
    runtime.register_process(WATCH_FILES, watch_files)


__all__ = [
    "EXTRACT_RESOURCE",
    "INTERPRET_RESOURCE",
    "RESOURCE_INDEXER",
    "WATCH_FILES",
    "bootstrap_observer",
    "bootstrap_resources",
    "extract_resource",
    "interpret_resource",
    "parse_facts",
    "resource_indexer",
    "watch_files",
]
