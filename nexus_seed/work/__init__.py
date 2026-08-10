"""Work intelligence domain data (not Runtime primitives).

* :class:`WorkRequirement` / :class:`WorkStatus` — a required unit of work.
* :class:`Impact` — the consequences of a state change.
* :class:`WorkMatch` / :class:`WorkMatchStatus` — matching against existing work.
* :mod:`rules` — deterministic impact rules and work->process registry.
* :func:`get_work_trace` — trace a process back to its raw event.
"""

from .impact import Impact
from .trace import WorkTrace, get_work_trace
from .work_match import WorkMatch, WorkMatchStatus
from .work_requirement import WorkRequirement, WorkStatus

__all__ = [
    "Impact",
    "WorkTrace",
    "get_work_trace",
    "WorkMatch",
    "WorkMatchStatus",
    "WorkRequirement",
    "WorkStatus",
]
