"""Context compilation: turn persistent Memory into a per-activation view.

``ContextRequirements`` is exported here (it has no ``core`` dependency, so it
is safe to import during ``core`` initialization).  ``ProcessContextView``,
``ContextSnapshot`` and ``ContextCompiler`` live in submodules and should be
imported from there to avoid import cycles during package initialization.
"""

from .requirements import (
    ContextRequirements,
    ContinuationReq,
    EntityAttributes,
    EventsReq,
    ProcessTreeReq,
    ResourcesReq,
    WorldStateReq,
)

__all__ = [
    "ContextRequirements",
    "ContinuationReq",
    "EntityAttributes",
    "EventsReq",
    "ProcessTreeReq",
    "ResourcesReq",
    "WorldStateReq",
]
