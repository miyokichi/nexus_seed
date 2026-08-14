"""Capability trace — why is this work waiting, and who was considered?

Answers the question a name-based registry never could: *the system decided it
could not do this — on what grounds?*  Every matching attempt is kept, so a
requirement that was blocked on Monday and ran on Tuesday shows both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.process import ProcessInstance
    from ..work.work_requirement import WorkRequirement
    from .models import CapabilityWorkMatch


@dataclass
class CapabilityTrace:
    """How one unit of work was matched (or not) to a process."""

    requirement: "WorkRequirement"
    attempts: list["CapabilityWorkMatch"] = field(default_factory=list)
    instances: list["ProcessInstance"] = field(default_factory=list)

    @property
    def latest(self) -> "CapabilityWorkMatch | None":
        """The most recent matching attempt."""
        return self.attempts[-1] if self.attempts else None

    @property
    def required_capabilities(self) -> list[str]:
        """What this work declared it needs."""
        return [r.name for r in self.requirement.required_capabilities]

    @property
    def missing_capabilities(self) -> list[str]:
        """What was missing at the most recent attempt."""
        return list(self.latest.missing_capabilities) if self.latest else []

    @property
    def candidates(self) -> list[dict]:
        """Every definition weighed at the most recent attempt."""
        return list(self.latest.candidates) if self.latest else []

    @property
    def selected(self) -> tuple[str, str] | None:
        """The chosen ``(definition_name, version)``, if one was."""
        if self.latest is None or self.latest.selected_definition_name is None:
            return None
        return (
            self.latest.selected_definition_name,
            self.latest.selected_definition_version,
        )

    @property
    def reasons(self) -> list[str]:
        """Why the most recent attempt concluded what it did."""
        return list(self.latest.reasons) if self.latest else []

    @property
    def was_blocked(self) -> bool:
        """Whether this work was ever held back for want of a capability."""
        return any(
            attempt.status.value != "MATCHED_SINGLE_PROCESS" for attempt in self.attempts
        )


def get_capability_trace(
    work_requirement_id,
    *,
    work_requirement_store,
    capability_store,
    process_store,
) -> CapabilityTrace | None:
    """Resolve how a work requirement was matched, from storage only."""
    requirement = work_requirement_store.get(work_requirement_id)
    if requirement is None:
        return None
    return CapabilityTrace(
        requirement=requirement,
        attempts=capability_store.matches_for(requirement.id),
        instances=process_store.find_by_work_requirement_id(requirement.id),
    )
