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
