"""Deterministic domain rules for work intelligence (no LLM, no planner).

This module is *domain* logic, deliberately kept out of the Runtime:

* :func:`expected_work_types` — which work a state change implies.
* :func:`build_work_key` — the logical identity of a required work item.
* :data:`WORK_PROCESS_REGISTRY` — which ProcessDefinition performs each work type.

The Runtime never imports these; only the work-intelligence processes do.
"""

from __future__ import annotations

#: Which ProcessDefinition (name, version) performs each work type.
WORK_PROCESS_REGISTRY: dict[str, tuple[str, str]] = {
    "resistance_check": ("resistance_check", "1"),
    "write_analysis_result": ("write_analysis_result", "1"),
}

#: Default priority per work type.
WORK_PRIORITY: dict[str, int] = {
    "resistance_check": 80,
    "write_analysis_result": 60,
}

#: What each work type *takes* to do, as capability names (Phase 4A).
#:
#: This is the replacement for :data:`WORK_PROCESS_REGISTRY` as the primary
#: mechanism: work now says what competence it needs, and the capability
#: registry finds a process that has it.  The name table stays for legacy
#: compatibility, and as the fallback when work declares no capabilities.
WORK_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "resistance_check": ("analyze_resistance",),
    "write_analysis_result": ("generate_analysis_report",),
}


def capabilities_for_work_type(work_type: str) -> list[str]:
    """Return the capability names a work type requires."""
    return list(WORK_CAPABILITIES.get(work_type, ()))


#: What a work type may start from, and what it must produce (Phase 4B).
#:
#: Only needed when the work might take several processes: composition uses
#: these to decide what can feed what, and when the job is actually done.
WORK_IO_TYPES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}


def io_types_for_work_type(work_type: str) -> tuple[list[str], list[str]]:
    """Return ``(available_input_types, required_output_types)`` for a work type."""
    inputs, outputs = WORK_IO_TYPES.get(work_type, ((), ()))
    return list(inputs), list(outputs)


def expected_work_types(entity: str, attribute: str) -> list[str]:
    """Return the work types a change to ``entity.attribute`` requires.

    Phase 2C rule: a change to a ``target`` attribute requires a
    ``resistance_check``.  Phase 3C adds a second rule whose work happens to
    reach *outside* the system — a recorded ``analysis_result`` must be written
    out — to show that acting on the world is just another kind of work, not a
    new mechanism.
    """
    if attribute == "target":
        return ["resistance_check"]
    if attribute == "analysis_result":
        return ["write_analysis_result"]
    return []


def build_work_key(work_type: str, entity: str, version: int) -> str:
    """Build a work item's logical identity, including the state version.

    Including ``version`` means work for ``D1_CD.target`` v2 and v3 are distinct
    work items, so a new state version yields new work.
    """
    return f"{work_type}:{entity}:v{version}"


def process_for_work_type(work_type: str) -> tuple[str, str] | None:
    """Return the ProcessDefinition (name, version) for ``work_type``, if any."""
    return WORK_PROCESS_REGISTRY.get(work_type)
