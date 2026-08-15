"""Regenerable Self, Master and Intention projections over existing stores."""

from __future__ import annotations

import uuid
from typing import Any

from .models import (
    ClaimStatus,
    IntentionRecord,
    MasterClaim,
    MasterProjection,
    SelfProjection,
    intention_id_for_goal,
)


MASTER_CATEGORIES = (
    "goals",
    "preferences",
    "projects",
    "commitments",
    "concerns",
    "shared_history",
)


def get_intention(runtime, intention_id: uuid.UUID | str) -> IntentionRecord | None:
    """Read one Intention from the World State current projection."""

    entry = runtime.state_store.get_current(f"intention:{intention_id}", "record")
    if entry is None or not isinstance(entry.value, dict):
        return None
    try:
        return IntentionRecord.from_dict(entry.value)
    except (KeyError, TypeError, ValueError):
        return None


def get_intention_for_goal(runtime, goal_id: uuid.UUID | str) -> IntentionRecord | None:
    """Read the stable Intention associated with a Goal."""

    return get_intention(runtime, intention_id_for_goal(goal_id))


def get_intentions(runtime, *, include_terminal: bool = True) -> list[IntentionRecord]:
    """Return all valid Intention records in deterministic id order."""

    intentions: list[IntentionRecord] = []
    for entry in runtime.state_store.all_current():
        if not entry.entity.startswith("intention:") or entry.attribute != "record":
            continue
        if not isinstance(entry.value, dict):
            continue
        try:
            intention = IntentionRecord.from_dict(entry.value)
        except (KeyError, TypeError, ValueError):
            continue
        if include_terminal or not intention.status.terminal:
            intentions.append(intention)
    return sorted(intentions, key=lambda value: str(value.id))


def project_self(runtime) -> SelfProjection:
    """Compile Self from World State, Goals, Intentions and CapabilityRegistry."""

    facts = {
        entry.attribute: entry.value
        for entry in runtime.state_store.current_for_entity("self")
    }
    goals = tuple(
        goal.id for goal in runtime.control_store.goals("ACTIVE")
    )
    intentions = tuple(get_intentions(runtime, include_terminal=False))
    capabilities = tuple(
        sorted(runtime.capabilities.provided_capability_names())
    )
    return SelfProjection(
        identity=facts.get("identity"),
        current_concerns=_tuple(facts.get("current_concerns")),
        commitments=_tuple(facts.get("commitments")),
        unresolved_questions=_tuple(facts.get("unresolved_questions")),
        beliefs=_tuple(facts.get("beliefs")),
        active_goal_ids=goals,
        active_intentions=intentions,
        available_capabilities=capabilities,
    )


def project_master(runtime, master_id: str) -> MasterProjection:
    """Compile Master claims, refusing values without an epistemic status."""

    grouped: dict[str, list[MasterClaim]] = {name: [] for name in MASTER_CATEGORIES}
    for entry in runtime.state_store.current_for_entity(f"master:{master_id}"):
        if not entry.attribute.startswith("claim:") or not isinstance(entry.value, dict):
            continue
        parts = entry.attribute.split(":", 2)
        if len(parts) != 3 or parts[1] not in grouped:
            continue
        try:
            status = ClaimStatus(str(entry.value["claim_status"]).upper())
        except (KeyError, ValueError):
            continue
        source = entry.value.get("source_event_id")
        grouped[parts[1]].append(
            MasterClaim(
                category=parts[1],
                key=parts[2],
                value=entry.value.get("value"),
                status=status,
                confidence=float(entry.value.get("confidence", entry.confidence)),
                source_event_id=uuid.UUID(str(source)) if source else entry.source_event,
                reason=str(entry.value.get("reason") or ""),
            )
        )
    for values in grouped.values():
        values.sort(key=lambda value: value.key)
    return MasterProjection(
        master_id=master_id,
        goals=tuple(grouped["goals"]),
        preferences=tuple(grouped["preferences"]),
        projects=tuple(grouped["projects"]),
        commitments=tuple(grouped["commitments"]),
        concerns=tuple(grouped["concerns"]),
        shared_history=tuple(grouped["shared_history"]),
    )


def _tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


__all__ = [
    "MASTER_CATEGORIES",
    "get_intention",
    "get_intention_for_goal",
    "get_intentions",
    "project_master",
    "project_self",
]
