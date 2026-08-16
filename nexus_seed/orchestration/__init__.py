"""Goal-driven orchestration: a thin coordination layer, never a new subsystem.

It reads where a Goal stands in the loop and states when the loop needs a
person.  Planning, capability acquisition, execution and evaluation stay with
the subsystems that already own them.
"""

from .loop import get_goal_loop, get_goal_loops
from .models import GoalLoopStage, GoalLoopStatus
from .processes import (
    HUMAN_INTERVENTION_REQUIRED,
    bootstrap_orchestration,
    request_human_intervention,
)

__all__ = [
    "GoalLoopStage",
    "GoalLoopStatus",
    "HUMAN_INTERVENTION_REQUIRED",
    "bootstrap_orchestration",
    "get_goal_loop",
    "get_goal_loops",
    "request_human_intervention",
]
