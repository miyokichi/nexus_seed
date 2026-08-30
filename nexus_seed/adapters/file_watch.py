"""Module alias for the local-file Observer adapter.

Aliasing, rather than copying names, preserves existing monkeypatch-based
adapter tests and integrations while the implementation has one owner.
"""

from importlib import import_module
import sys

_implementation = import_module("nexus_seed.modules.observer.adapters.file_watch")
sys.modules[__name__] = _implementation
