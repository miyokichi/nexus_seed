"""Human Interface Layer over existing NEXUS SEED projections and traces."""

from .service import CockpitService, humanize_error

__all__ = ["CockpitService", "humanize_error"]
