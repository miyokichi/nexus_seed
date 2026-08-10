"""Semantic world model (domain data, not Runtime primitives).

* :class:`Observation` — what a process read from a raw event.
* :class:`StateDelta` — a proposed semantic change to world state.
* :class:`StateConflict` — a delta disagreeing with current state.
* :func:`get_state_provenance` — walk a fact back to its raw event.
"""

from .observation import Observation
from .provenance import Provenance, get_state_provenance
from .state_delta import StateConflict, StateDelta

__all__ = [
    "Observation",
    "StateDelta",
    "StateConflict",
    "Provenance",
    "get_state_provenance",
]
