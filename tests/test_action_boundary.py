"""The Phase 3C invariants themselves (spec §42, §73), tested as behaviour.

* **21/22** — a Process reaches the world only through an ActionProposal.
* **23** — a proposal always passes permission + risk policy.
* **24** — results return as Events.
* **25** — a backend never writes World State.
* **26** — failed attempts stay in the journal.
* **27** — one proposal, at most one effect.

The first two are structural: what makes them true is that a handler is *given*
nothing that can act.  So that is what is asserted here.
"""

from __future__ import annotations

from action_helpers import do_action, fake_runtime, instances_named

from nexus_seed.actions.models import ActionProposal, ActionProposalStatus, RiskLevel
from nexus_seed.backends import FakeActionBackend
from nexus_seed.context.requirements import ContextRequirements
from nexus_seed.core.process import ProcessDefinition
from nexus_seed.runtime.runtime import Runtime

SEEN: dict = {}

PEEK = ProcessDefinition(
    name="peek",
    version="1",
    handler="peek",
    trigger_event_types=("peek",),
    metadata={"permissions": ["filesystem.write"]},
    context_requirements=ContextRequirements(include_trigger_event=True),
)


async def peek_handler(ctx):
    """Record what a handler can actually reach, then do nothing."""
    SEEN["view_type"] = type(ctx.view).__name__
    SEEN["can_write_state_directly"] = hasattr(ctx.view, "set")
    SEEN["services_writers"] = [
        name
        for name in dir(ctx.services)
        if not name.startswith("_") and not name.startswith("get") and not name.startswith("find")
        and not name.startswith("active") and not name.startswith("completed")
    ]
    return ctx.complete(output={})


async def test_a_handler_is_given_no_way_to_write_state_or_act(tmp_path):
    """Invariants 21/25: the context is read-only; effects are returned, not done."""
    runtime = Runtime(tmp_path / "peek.db")
    fake_runtime(runtime)
    runtime.register_process(PEEK, peek_handler)

    from nexus_seed.core.event import Event

    await runtime.submit_event(Event("peek", "test", {}))

    assert SEEN["view_type"] == "ProcessContextView"
    assert SEEN["can_write_state_directly"] is False
    # RuntimeServices exposes reads only — no mutating entry points.
    assert SEEN["services_writers"] == []
    runtime.close()


async def test_the_action_backend_is_registered_but_only_the_executor_uses_it(tmp_path):
    """Invariant 22: a proposing process never calls the backend it named."""
    runtime = Runtime(tmp_path / "boundary.db")
    backend = fake_runtime(runtime)

    await runtime.submit_event(do_action(target="out.txt"))

    # The only process that touched the backend is the action_executor.
    executors = instances_named(runtime, "action_executor")
    assert len(executors) == 1
    assert len(backend.calls) == 1
    assert backend.calls[0].metadata["process_instance_id"] == str(executors[0].id)
    runtime.close()


async def test_every_proposal_carries_a_decision_before_any_execution(tmp_path):
    """Invariant 23: nothing runs that has no recorded authorization."""
    runtime = Runtime(tmp_path / "authz.db")
    fake_runtime(runtime)

    for risk in ("LOW", "HIGH", "CRITICAL"):
        await runtime.submit_event(do_action(target=f"{risk}.txt", risk_level=risk))

    for proposal in runtime.get_action_proposals():
        decisions = runtime.get_action_decisions(proposal.id)
        assert decisions, f"proposal {proposal.id} has no decision record"
        executions = runtime.get_action_executions(proposal.id)
        if executions:
            # An execution implies an APPROVE came first.
            assert decisions[0].decision.value == "APPROVE"
    runtime.close()


async def test_an_unapproved_proposal_cannot_be_executed_by_a_forged_event(tmp_path):
    """Invariant 23: the executor re-checks status; it does not trust the event."""
    from nexus_seed.core.event import Event

    runtime = Runtime(tmp_path / "forge.db")
    backend = fake_runtime(runtime)

    await runtime.submit_event(do_action(target="held.txt", risk_level="HIGH"))
    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.REVIEW

    # Someone injects an action_approved for a proposal still under review.
    await runtime.submit_event(
        Event(
            "action_approved",
            "forged",
            {
                "action_proposal_id": str(proposal.id),
                "root_proposal_id": str(proposal.root_proposal_id),
            },
        )
    )

    assert backend.calls == []
    assert runtime.get_action_executions(proposal.id) == []
    assert runtime.get_action_proposal(proposal.id).status is ActionProposalStatus.REVIEW
    runtime.close()


def test_a_backend_cannot_reach_runtime_or_domain_state():
    """Invariant 19/25: the backend interface takes and returns plain data."""
    backend = FakeActionBackend()
    assert set(vars(backend)) == {"script", "default", "_capabilities", "calls", "performed"}
    # Nothing store-shaped, runtime-shaped or state-shaped is reachable from it.
    assert not any(
        hasattr(backend, name)
        for name in ("state_store", "runtime", "process_store", "services")
    )


def test_a_proposal_is_not_an_action():
    """The type itself refuses to conflate intention with effect."""
    proposal = ActionProposal(
        backend="fake_action", action_type="write_file", risk_level=RiskLevel.LOW
    )
    assert proposal.status is ActionProposalStatus.PENDING
    assert not hasattr(proposal, "execute")
    assert not hasattr(proposal, "run")
