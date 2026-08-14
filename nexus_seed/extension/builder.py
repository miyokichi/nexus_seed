"""Turning a candidate route into a proposal — with or without a model.

Two builders, and the order between them is the same one Phase 4C drew between
its selectors (Invariant 77):

* :func:`build_proposal` is deterministic and always available.  Given a gap and
  the analyzer's best candidate it writes a plain, derived description.  With no
  LLM configured at all, NEXUS SEED still notices what it is missing, still
  works out how it could be acquired, and still asks whether that may proceed
  (spec §82).
* :class:`LLMExtensionProposer` asks a model to *elaborate* one of the candidate
  routes — a better title, a real description, a sensible decomposition into
  components (spec §83).  It cannot introduce a strategy the analyzer did not
  find, cannot choose what the gap is, and cannot approve anything: its answer
  is a proposal that goes through validation, risk classification and policy
  like any other (Invariant 87).

What the model is *not* asked for is as deliberate as what it is: no shell
commands (spec §32), no patches, no source code (spec §33). Those belong to
construction, which is Phase 5B, and asking for them here would produce
material that looks executable while nothing in this phase may execute it.
"""

from __future__ import annotations

from ..backends.base import BackendRequest
from .models import (
    SOURCE_DETERMINISTIC,
    SOURCE_LLM,
    AcquisitionCandidate,
    AcquisitionFeasibility,
    ExtensionProposal,
    ExtensionStrategy,
)
from .strategies import classify_risk, implied_permissions

#: All the model is asked for (spec §34).  Note what is absent: no commands,
#: no file paths, no code, and no capability names — targets come from the gap.
EXTENSION_SCHEMA = {
    "strategy": "str",
    "title": "str",
    "description": "str",
    "proposed_components": [
        {
            "component_type": "str",
            "name": "str",
            "purpose": "str",
            "provides_capabilities": ["str"],
            "requires_capabilities": ["str"],
            "required_permissions": ["str"],
        }
    ],
    "required_permissions": ["str"],
    "estimated_risk": "LOW|MEDIUM|HIGH|CRITICAL",
    "rationale": "str",
}

INSTRUCTION = (
    "This system has found work it cannot currently do, and has already worked "
    "out which acquisition strategies are actually available to it. Choose one "
    "of the given strategies and describe the extension it implies: a title, a "
    "description, and the components that would have to be added. "
    "Answer only with one of the given strategies. Do not invent a strategy, do "
    "not propose changes to the runtime core or to the permission policy, do "
    "not write code, patches or shell commands, and do not claim the extension "
    "provides capabilities other than the ones listed as targets."
)


def build_proposal(
    gap,
    candidate: AcquisitionCandidate,
    *,
    candidates=(),
    work_requirement_id=None,
    created_by_process_id=None,
    context_snapshot_id=None,
    source: str = SOURCE_DETERMINISTIC,
) -> ExtensionProposal:
    """Derive a proposal from one candidate route — no model involved.

    Everything here comes from the analysis: the strategy, the components, the
    permissions the strategy implies and the risk those imply in turn.  The
    prose is dull by design; it is derived material, and dressing it up would
    make a derived description indistinguishable from a written one.
    """
    targets = [r for r in gap.missing_capabilities if r.name in candidate.target_capabilities]
    permissions = implied_permissions(candidate.strategy, candidate.required_new_components)
    risk = classify_risk(
        candidate.strategy,
        components=candidate.required_new_components,
        permissions=permissions,
    )
    names = ", ".join(sorted(candidate.target_capabilities))
    proposal = ExtensionProposal(
        capability_gap_id=gap.id,
        work_requirement_id=work_requirement_id or gap.work_requirement_id,
        target_capabilities=targets,
        strategy=candidate.strategy,
        title=f"{candidate.strategy.value.lower().replace('_', ' ')}: {names}",
        description=_describe(candidate, names),
        proposed_components=list(candidate.required_new_components),
        reusable_components=list(candidate.reusable_components),
        required_permissions=permissions,
        candidate_strategies=[c.strategy.value for c in candidates] or [
            candidate.strategy.value
        ],
        analysis={"candidates": [c.summary() for c in candidates] or [candidate.summary()]},
        estimated_risk=risk,
        estimated_cost=candidate.estimated_cost,
        feasibility=candidate.feasibility,
        rationale="; ".join(candidate.reasons) or None,
        source=source,
        created_by_process_id=created_by_process_id,
        context_snapshot_id=context_snapshot_id,
    )
    return proposal


def _describe(candidate: AcquisitionCandidate, names: str) -> str:
    """A derived, factual description of what the route would involve."""
    parts = [f"Acquire {names} by {candidate.strategy.value}."]
    if candidate.reusable_components:
        parts.append("Reuses " + ", ".join(sorted(candidate.reusable_components)) + ".")
    if candidate.required_new_components:
        added = ", ".join(
            f"{c.name} ({c.component_type})" for c in candidate.required_new_components
        )
        parts.append(f"Would add {added}.")
    else:
        parts.append("Adds nothing new.")
    return " ".join(parts)


class LLMExtensionProposer:
    """Asks a model to elaborate one of the analyzer's candidate routes.

    The candidate list it is shown *is* the boundary of its answer (spec §35).
    A reply naming something else is not a bolder proposal, it is a proposal
    about a system that does not exist, and validation refuses it by name.
    """

    name = "llm"
    version = "1"

    def __init__(self, backend, *, max_candidates: int = 4) -> None:
        self.backend = backend
        self.max_candidates = max_candidates

    def shortlist(self, candidates) -> list[AcquisitionCandidate]:
        """The routes to show — already ordered, truncated from the worst end.

        Truncation drops the *least* reusing candidates, never an arbitrary
        subset: which options a model was allowed to see must not depend on
        dictionary order (the same rule as Phase 4C's shortlist).
        """
        usable = [
            c
            for c in candidates
            if c.strategy is not ExtensionStrategy.UNSUPPORTED
            and c.feasibility is not AcquisitionFeasibility.UNSUPPORTED
        ]
        return usable[: self.max_candidates]

    def build_request(
        self, gap, candidates, *, context: dict | None = None, work_summary=None
    ) -> BackendRequest:
        """The prompt: this gap, this work, and these available routes.

        Deliberately narrow (spec §53).  The whole capability registry and every
        process definition are *not* sent: what is sent is the gap and the
        routes the analyzer found for it, which is all the question needs.
        """
        return BackendRequest(
            instruction=INSTRUCTION,
            context=context or {},
            output_schema=EXTENSION_SCHEMA,
            metadata={
                "capability_gap": gap.summary(),
                "work": work_summary or {},
                "candidate_strategies": [c.strategy.value for c in candidates],
                "candidates": [c.summary() for c in candidates],
                "target_capabilities": gap.missing_names,
            },
        )

    async def propose(
        self,
        gap,
        candidates,
        *,
        work_requirement_id=None,
        created_by_process_id=None,
        context: dict | None = None,
        work_summary=None,
    ):
        """Ask for an elaboration.  Returns ``(proposal, backend_result, request)``.

        ``proposal is None`` means the answer could not be read as a proposal at
        all — a schema failure, which the caller may retry.  An answer that
        *is* readable but names an unknown strategy or component is returned, so
        it is refused on the record rather than retried forever (spec §102).
        """
        request = self.build_request(
            gap, candidates, context=context, work_summary=work_summary
        )
        result = await self.backend.execute(request)
        if not result.success:
            return None, result, request
        proposal = ExtensionProposal.from_output(
            result.parsed_output,
            capability_gap_id=gap.id,
            work_requirement_id=work_requirement_id or gap.work_requirement_id,
            target_capabilities=list(gap.missing_capabilities),
            candidate_strategies=[c.strategy.value for c in candidates],
            created_by_process_id=created_by_process_id,
            source=SOURCE_LLM,
        )
        if proposal is not None:
            proposal.analysis = {"candidates": [c.summary() for c in candidates]}
        return proposal, result, request


__all__ = [
    "EXTENSION_SCHEMA",
    "INSTRUCTION",
    "LLMExtensionProposer",
    "build_proposal",
]
