"""Agent construction from a profile.

The one place an :class:`~little_agent.agent.Agent` is assembled, so the CLI and
the A2A server build the *same* runtime and differ only in what they pass:
chat supplies a memory store and a session, a served task supplies neither.
"""

from __future__ import annotations

from dataclasses import replace

from little_agent.agent import Agent
from little_agent.agents import AgentProfile
from little_agent.config import AgentConfig
from little_agent.control import StopController
from little_agent.memory.store import MemoryStore, NullMemoryStore
from little_agent.skills.loader import SkillLoader


def build_agent(
    config: AgentConfig,
    profile: AgentProfile,
    confirm,
    stop: StopController,
    depth: int = 0,
    memory: MemoryStore | None = None,
) -> Agent:
    """Construct an Agent for a profile (use agents.default_profile for the library).

    Profile overrides (model / max_tool_steps / require_confirmation) are layered
    onto the base config; ``core_tools`` filters the built-in core tools; and the
    skill loader is pointed at the skills the profile declares.

    ``memory`` is the persistence the agent gets. It defaults to
    :class:`~little_agent.memory.store.NullMemoryStore`, so an agent remembers
    nothing unless a caller deliberately hands it a store — which is what makes
    ``chat --no-memory`` and A2A serving safe by construction rather than by
    remembering to switch something off.

    ``depth`` is the A2A delegation depth of this agent. While it is below
    ``config.max_delegation_depth`` — and the profile allows the tools — the agent
    gets ``delegate_task``/``delegate_tasks`` so it can hand subtasks to peer
    agents over A2A; at the limit they are omitted.
    """

    effective = replace(
        config,
        model=profile.model or config.model,
        max_tool_steps=(
            profile.max_tool_steps if profile.max_tool_steps is not None else config.max_tool_steps
        ),
        require_confirmation=(
            profile.require_confirmation
            if profile.require_confirmation is not None
            else config.require_confirmation
        ),
    )
    skills = SkillLoader(
        [root.resolve() for root in profile.skill_roots()], names=profile.skill_names()
    )
    agent = Agent(
        config=effective,
        skills=skills,
        confirm=confirm,
        stop=stop,
        core_tools=profile.core_tools_set(),
        memory=memory or NullMemoryStore(),
    )

    if depth < config.max_delegation_depth:
        # Imported lazily: the delegation tools pull in the A2A client stack,
        # which in turn may spawn servers that import this module.
        from little_agent.tools.delegation import DelegateTasksTool, DelegateTaskTool

        # ``stop`` is passed so a long delegation is abandoned (and the peer's
        # task cancelled) when the emergency-stop hotkey fires. The delegation
        # tools see ``effective`` so a granted workspace is what they can hand on.
        for tool in (
            DelegateTaskTool(config=effective, depth=depth, stop=stop),
            DelegateTasksTool(config=effective, depth=depth, stop=stop),
        ):
            if profile.tool_allowed(tool.name):
                agent.tools.register(tool)
    return agent
