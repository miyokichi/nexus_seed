"""Chat: a conversation on top of the shared agent runtime.

The runtime keeps nothing between runs, so continuity lives here. A session owns
the transcript and hands it back to :meth:`Agent.run` each turn; that is the only
difference between a chat turn and an A2A task, which passes no history and so
starts clean every time.

Persistence is a separate axis, and it is the store that decides:

* ``chat``               -> :class:`~little_agent.memory.store.FileMemoryStore`
* ``chat --no-memory``   -> :class:`~little_agent.memory.store.NullMemoryStore`
* A2A                    -> :class:`~little_agent.memory.store.NullMemoryStore`

With memory off the transcript still works for the length of the session; nothing
is loaded at the start and nothing is written at the end.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from little_agent.agent import Agent, StructuredOutputError
from little_agent.messages import Message

if TYPE_CHECKING:
    from little_agent.memory.store import MemoryStore


class ChatSession:
    """One interactive conversation with one agent.

    ``history`` holds the user/assistant turns only. Tool traffic belongs to the
    run that produced it and is not replayed: it would grow without bound and add
    little the final answer does not already carry.
    """

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.history: list[Message] = []
        # How much of the transcript the last reflection saw, so ``learn`` is a
        # no-op when nothing has been said since.
        self._learned_upto = 0

    @property
    def memory(self) -> "MemoryStore":
        return self.agent.memory

    def switch_agent(self, agent: Agent) -> None:
        """Continue this conversation with a different agent.

        The transcript carries over — the user is still talking about the same
        thing — but the reflection watermark does not reset, so a switch mid-way
        does not cause the earlier turns to be learned twice.
        """

        self.agent = agent

    def send(self, text: str) -> str:
        """Run one turn and return the answer, recording both sides."""

        try:
            result = self.agent.run(text, history=self.history)
        except StructuredOutputError as exc:
            return f"Structured output error: {exc}"
        answer = result.text
        self.history.append(Message(role="user", content=text))
        self.history.append(Message(role="assistant", content=answer))
        return answer

    def clear(self) -> int:
        """Drop the transcript and start fresh. Returns how many turns went."""

        dropped = len(self.history)
        self.history.clear()
        self._learned_upto = 0
        return dropped

    def learn(self) -> bool:
        """Distil the conversation so far into durable memory.

        Called at the end of a session and by ``/remember``. Returns True when
        something was actually written, which is never the case with memory off.
        """

        if len(self.history) <= self._learned_upto:
            return False
        # Mark progress even when the store declines, so a no-op is not retried.
        learned = self.memory.learn(self.history, self.agent.llm, self.agent.config.model)
        self._learned_upto = len(self.history)
        if learned and self.agent.logger:
            self.agent.logger.log_conversation("auto_learning", turns=len(self.history))
        return learned
