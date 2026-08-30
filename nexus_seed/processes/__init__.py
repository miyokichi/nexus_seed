"""Process handlers used by the Project-centered application."""

from .project_orchestration import (
    ROUTE_REQUEST,
    bootstrap_project_orchestration,
    route_request_to_project,
)
from .resources import (
    EXTRACT_RESOURCE,
    RESOURCE_INDEXER,
    WATCH_FILES,
    bootstrap_observer,
    bootstrap_resources,
    extract_resource,
    resource_indexer,
    watch_files,
)

__all__ = [
    "EXTRACT_RESOURCE",
    "RESOURCE_INDEXER",
    "ROUTE_REQUEST",
    "WATCH_FILES",
    "bootstrap_observer",
    "bootstrap_project_orchestration",
    "bootstrap_resources",
    "extract_resource",
    "resource_indexer",
    "route_request_to_project",
    "watch_files",
]
