"""NEXUS SEED application composition and command-line entry points.

The application module is loaded lazily so importing a reusable flow does not
eagerly construct the entire composition root.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "AppSettings",
    "ApplicationConfigurationError",
    "bootstrap_application",
    "build_parser",
    "build_runtime",
    "main",
    "run",
]


def main(argv: list[str] | None = None) -> int:
    """Run the application without eagerly loading composition on import."""
    return import_module(".application", __name__).main(argv)


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    return getattr(import_module(".application", __name__), name)
