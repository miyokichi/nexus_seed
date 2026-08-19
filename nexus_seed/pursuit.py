"""What NEXUS SEED is currently trying to achieve, whatever supplies it.

Phase 6 needs four things about a pursuit to hold an Intention about it: an
identity, what it is for, whether it is still live, and what should make it
reconsider.  A Goal has those; an orchestrator Project has them too.  Naming
them here is what lets the source change without Phase 6 changing.

Identities are strings on purpose.  A Goal's id is a UUID and a Project's is
``project-<uuid>``; the layer above only needs them to be stable and comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Pursuit:
    """One thing being pursued, described only as far as Phase 6 needs it."""

    id: str
    objective: str
    title: str = ""
    #: Whether this is still being pursued.  A finished or abandoned pursuit is
    #: still describable — it is simply no longer live.
    active: bool = True
    #: Event types that should make the Intention about this be reconsidered.
    reconsider_on: tuple[str, ...] = ()
    #: Whatever the supplying domain wants to carry along.  Phase 6 does not
    #: interpret it; it is here so provenance is not lost in translation.
    source: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """A human label, falling back to the objective when unnamed."""

        return self.title or self.objective


class PursuitSource:
    """What a domain registers to answer "what are we pursuing?".

    Calling it lists the live pursuits; :meth:`get` resolves one by id whether
    it is live or not, because an Intention must keep describing a pursuit that
    has just finished.  Subclasses implement :meth:`live` and :meth:`get`.
    """

    def live(self) -> list[Pursuit]:
        raise NotImplementedError

    def get(self, pursuit_id: str) -> Pursuit | None:
        raise NotImplementedError

    def __call__(self) -> list[Pursuit]:
        return self.live()


__all__ = ["Pursuit", "PursuitSource"]
