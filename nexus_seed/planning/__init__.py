"""Dynamic process composition — several processes, one job.

    COMPOSITION_REQUIRED -> CompositionPlanner -> PlanCandidate
                         -> PlanValidator      -> ProcessPlan (durable)
                         -> plan execution via ordinary spawn / join

Plans are domain data.  A PlanNode names a ProcessDefinition and a position; a
ProcessInstance is still the only thing that runs.
"""

from .models import (
    BindingStatus,
    PlanCandidate,
    PlanEdge,
    PlanNode,
    PlanNodeStatus,
    PlanStatus,
    PlanValidation,
    Port,
    ProcessPlan,
    SearchBounds,
    TypedOutput,
    parse_ports,
    typed_outputs_of,
)
from .planner import CompositionPlanner
from .trace import PlanBinding, PlanNodeTrace, PlanTrace, get_plan_trace
from .validation import PlanValidator, resolve_legacy_binding

__all__ = [
    "BindingStatus",
    "CompositionPlanner",
    "PlanBinding",
    "PlanCandidate",
    "PlanEdge",
    "PlanNode",
    "PlanNodeStatus",
    "PlanNodeTrace",
    "PlanStatus",
    "PlanTrace",
    "PlanValidation",
    "PlanValidator",
    "Port",
    "ProcessPlan",
    "SearchBounds",
    "TypedOutput",
    "get_plan_trace",
    "parse_ports",
    "resolve_legacy_binding",
    "typed_outputs_of",
]
