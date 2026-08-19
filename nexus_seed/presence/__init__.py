"""Phase 6 persistent-being domain views built from existing primitives.

Nothing in this package is a Core primitive.  Self and Master are projections
over durable domain records, Intention is a World State schema, and Experience
is reconstructed from Events and the existing audit trail.
"""

from .models import (
    AttentionDisposition,
    ClaimStatus,
    ExperienceRecord,
    IntentionRecord,
    IntentionStatus,
    MasterClaim,
    MasterProjection,
    SelfProjection,
    self_question_id,
    intention_id_for_pursuit,
)
from .projections import (
    get_intention,
    get_intentions,
    project_master,
    project_self,
)
from .trace import ExperienceTrace, get_experience_trace

__all__ = [
    "AttentionDisposition",
    "ClaimStatus",
    "ExperienceRecord",
    "ExperienceTrace",
    "IntentionRecord",
    "IntentionStatus",
    "MasterClaim",
    "MasterProjection",
    "SelfProjection",
    "self_question_id",
    "get_experience_trace",
    "get_intention",
    "get_intentions",
    "intention_id_for_pursuit",
    "project_master",
    "project_self",
]
