"""Agent2Agent (A2A) protocol support for Little Agent.

A2A is Little Agent's external API: there is no bespoke REST layer. It speaks the
protocol in both directions:

- **Server** (``little_agent.a2a.server``): publishes an Agent Card at
  ``/.well-known/agent-card.json`` and serves JSON-RPC 2.0 ``message/send``,
  ``tasks/get`` and ``tasks/cancel`` over HTTP, running one fresh agent per task.
  Requests and results carry either TextParts or DataParts.
- **Client** (``little_agent.a2a.client``): discovers a remote agent by URL and
  drives a task to a terminal state. The ``delegate_task`` tool uses it to hand
  work to peer agents — the peer can be any A2A-compliant agent, not just
  another Little Agent.
"""

from little_agent.a2a.models import (
    PROTOCOL_VERSION,
    TERMINAL_STATES,
    A2AError,
    RequestPayload,
    agent_card,
    data_part,
    parse_request_parts,
    parts_to_data,
    parts_to_text,
    task_result_data,
    task_result_text,
    text_part,
)

__all__ = [
    "A2AError",
    "PROTOCOL_VERSION",
    "RequestPayload",
    "TERMINAL_STATES",
    "agent_card",
    "data_part",
    "parse_request_parts",
    "parts_to_data",
    "parts_to_text",
    "task_result_data",
    "task_result_text",
    "text_part",
]
