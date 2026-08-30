"""Persistent memory, kept at arm's length from the agent runtime.

The runtime never touches a file: it talks to a :class:`MemoryStore`, and which
store it gets decides whether anything is remembered at all.

    MemoryStore (protocol)
    ├─ FileMemoryStore   markdown files on disk (chat with memory on)
    └─ NullMemoryStore   remembers nothing (chat --no-memory, and every A2A task)

A store contributes three things to a run: prompt sections (what the agent knows
before it starts), tools (how the agent writes something down), and reflection
(distilling a finished session into durable profiles).
"""

from little_agent.memory.reflection import PROFILE_MAX_CHARS, Reflector
from little_agent.memory.store import (
    FileMemoryStore,
    MemoryFile,
    MemoryStore,
    NullMemoryStore,
)

__all__ = [
    "FileMemoryStore",
    "MemoryFile",
    "MemoryStore",
    "NullMemoryStore",
    "PROFILE_MAX_CHARS",
    "Reflector",
]
