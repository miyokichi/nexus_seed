"""Shared scaffolding for the Phase 3C action tests (not a test module).

Provides a minimal "proposer" Process: it is triggered by a ``do_action``
event, turns the payload into an ActionProposal, and suspends until the action
boundary reports back.  That is exactly the contract a real action-capable
process has (Invariants 21-22), so the tests exercise the boundary rather than
a bypass of it.
"""

from __future__ import annotations

from nexus_seed.backends import FakeActionBackend, FakeLLMBackend, LocalFileActionBackend
from nexus_seed.context.requirements import ContextRequirements, ContinuationReq
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition, ProcessStatus
from nexus_seed.processes.actions import (
    action_proposed_event,
    bootstrap_actions,
    waiting_for_action,
)
from nexus_seed.processes.llm_interpret import bootstrap_llm_interpreter
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence

PROPOSER_NAME = "test_proposer"


def proposer_definition(*, permissions=("filesystem.write",)) -> ProcessDefinition:
    """A process granted ``permissions`` that proposes whatever it is asked to."""
    return ProcessDefinition(
        name=PROPOSER_NAME,
        version="1",
        handler=PROPOSER_NAME,
        trigger_event_types=("do_action",),
        metadata={"role": "work", "permissions": list(permissions)},
        context_requirements=ContextRequirements(
            include_trigger_event=True, continuation=ContinuationReq(include=True)
        ),
    )


async def proposer_handler(ctx):
    """Propose the action described by the trigger event, then wait for it."""
    if ctx.resume_point == "await_action":
        return ctx.complete(
            output={"outcome": ctx.event.type, "payload": dict(ctx.event.payload)}
        )

    p = ctx.event.payload
    proposal = ctx.propose_action(
        backend=p.get("backend", "fake_action"),
        action_type=p.get("action_type", "write_file"),
        target=p.get("target", "out.txt"),
        parameters=p.get("parameters", {"content": "hello"}),
        required_permissions=p.get("required_permissions", ["filesystem.write"]),
        declared_side_effects=p.get("declared_side_effects", ["filesystem_write"]),
        risk_level=p.get("risk_level", "LOW"),
        rationale="test proposal",
    )
    return ctx.suspend(
        resume_point="await_action",
        waiting_for=waiting_for_action(proposal),
        saved_process_state={"action_proposal_id": str(proposal.id)},
        emitted_events=[action_proposed_event(ctx, proposal)],
    )


def bootstrap(runtime, *, backends=None, permissions=("filesystem.write",), policy=None):
    """Register the action pipeline, the proposer and the given backends."""
    bootstrap_actions(runtime, policy=policy)
    runtime.register_process(proposer_definition(permissions=permissions), proposer_handler)
    for name, backend in (backends or {}).items():
        runtime.register_backend(name, backend)
    return runtime


def fake_runtime(runtime, *, script=None, permissions=("filesystem.write",), policy=None):
    """Bootstrap ``runtime`` with a :class:`FakeActionBackend` and return it."""
    backend = FakeActionBackend(script=script)
    bootstrap(runtime, backends={"fake_action": backend}, permissions=permissions, policy=policy)
    return backend


def file_runtime(runtime, root, *, permissions=("filesystem.write",), policy=None):
    """Bootstrap ``runtime`` with a sandboxed :class:`LocalFileActionBackend`."""
    backend = LocalFileActionBackend(root)
    bootstrap(runtime, backends={"local_file": backend}, permissions=permissions, policy=policy)
    return backend


# --- the whole perception -> work -> action stack ---------------------------

ANALYSIS_MESSAGE = (
    "D1のCD解析が完了しました。結果をレポートとして書き出してください。"
)


def analysis_proposal(confidence: float = 0.95, value: str = "within spec") -> dict:
    """LLM output that records an ``analysis_result`` on ``D1_CD``.

    A change to ``analysis_result`` is the rule that requires the outward-acting
    ``write_analysis_result`` work (see ``nexus_seed.work.rules``).
    """
    return {
        "subject": "D1_CD",
        "predicate": "analysis_completed",
        "confidence": confidence,
        "rationale": "the message reports a completed CD analysis",
        "proposed_state_deltas": [
            {
                "entity": "D1_CD",
                "attribute": "analysis_result",
                "old_value": None,
                "new_value": value,
                "unit": None,
                "confidence": confidence,
            }
        ],
    }


def full_stack(runtime, root, *, llm_script=None, policy=None):
    """Register perception, work intelligence and the action boundary.

    Returns the :class:`LocalFileActionBackend` so a test can inspect the
    calls that reached the outside world.
    """
    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)
    bootstrap_actions(runtime, policy=policy)
    bootstrap_llm_interpreter(runtime, FakeLLMBackend(script=llm_script))
    backend = LocalFileActionBackend(root)
    runtime.register_backend("local_file", backend)
    return backend


def human_message(text: str = ANALYSIS_MESSAGE) -> Event:
    """The raw world event that starts the whole loop."""
    return Event("human_message", "user", {"text": text})


def do_action(**payload) -> Event:
    """Build the event that starts the proposer."""
    return Event("do_action", "test", payload)


def proposer_instance(runtime):
    """Return the (single) proposer instance in ``runtime``."""
    return [
        i for i in runtime.process_store.all_instances() if i.definition_name == PROPOSER_NAME
    ][0]


def instances_named(runtime, name) -> list:
    """Return every instance of the definition ``name``, oldest first."""
    return [i for i in runtime.process_store.all_instances() if i.definition_name == name]


def suspended_validator(runtime):
    """Return the action_validator instance suspended for human review."""
    return [
        i
        for i in instances_named(runtime, "action_validator")
        if i.status is ProcessStatus.SUSPENDED
    ][0]
