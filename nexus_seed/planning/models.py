"""ProcessPlan — a durable, composed way to do work no single process can.

Phase 4A could ask *who can do this?* and answer with one process or a recorded
gap.  Phase 4B takes the gap it called ``COMPOSITION_REQUIRED`` — every needed
competence exists, but scattered across several processes — and works out an
order in which they add up to the whole job.

The load-bearing distinction (Invariant 56)::

    PlanNode          "use this ProcessDefinition, here, for this capability"
    ProcessInstance   the thing that actually runs

A node is a *position in a plan*, not an execution.  Keeping them apart is what
lets a plan be written down, validated, argued with and restarted without
anything having happened yet — and what lets a restart re-attach to the
instance a node already produced instead of starting a second one.

Nothing here is a core primitive.  Plans are domain data; execution is still
ordinary Processes driven by the existing spawn / join / continuation
machinery (spec §2, §34).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..core.event import utcnow


class PlanStatus(str, Enum):
    """Lifecycle of a composed plan.

    ``PROPOSED`` output goes to the validator before anything runs — the same
    proposal boundary Phases 3B and 3C established for interpretations and
    actions (spec §69).  ``BLOCKED`` is a plan that was valid when written but
    cannot proceed now (a definition was disabled since); it is distinct from
    ``WorkStatus.BLOCKED_CAPABILITY``, which is about the *need*, not the plan.
    """

    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    #: Phase 4C: a valid candidate that was considered and not chosen.  Kept
    #: rather than deleted (spec §41) — the options not taken are half of what
    #: makes a decision reviewable.
    SUPERSEDED = "SUPERSEDED"

    @property
    def active(self) -> bool:
        """Whether this plan is still the one being pursued."""
        return self in (PlanStatus.VALIDATED, PlanStatus.RUNNING)

    @property
    def terminal(self) -> bool:
        """Whether this plan will not progress further on its own."""
        return self in (
            PlanStatus.COMPLETED,
            PlanStatus.FAILED,
            PlanStatus.CANCELLED,
            PlanStatus.SUPERSEDED,
        )

    @property
    def selectable(self) -> bool:
        """Whether this plan could still be chosen to run (Phase 4C).

        ``PROPOSED`` is where a candidate waits: composed and validated on
        paper, but not yet the one.  Only selection moves it to ``VALIDATED``,
        which is what keeps "a plan exists" and "a plan was chosen" apart
        (spec §39).
        """
        return self in (PlanStatus.PROPOSED, PlanStatus.VALIDATED)


class PlanNodeStatus(str, Enum):
    """Lifecycle of one position in a plan."""

    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class Port:
    """A named point where data enters or leaves a process.

    Phase 4B connected processes by type alone, which is ambiguous the moment
    two producers offer the same type.  A port adds an optional *key*, so a
    process that consumes two measurements can say which is which::

        measurement            any measurement
        measurement:measured   the one called "measured"

    Written as ``"type"`` or ``"type:key"`` wherever a capability declares its
    input and output types, so nothing about the capability model had to change
    (spec §9).
    """

    type: str
    key: str | None = None

    @classmethod
    def parse(cls, spec: "str | Port") -> "Port":
        """Parse ``"type"`` or ``"type:key"`` (a Port passes through)."""
        if isinstance(spec, Port):
            return spec
        text = str(spec)
        if ":" in text:
            type_part, _, key_part = text.partition(":")
            return cls(type=type_part.strip(), key=key_part.strip() or None)
        return cls(type=text.strip())

    @property
    def name(self) -> str:
        """The key this port binds under — its key, or its bare type."""
        return self.key or self.type

    def matches(self, other: "Port") -> bool:
        """Whether ``other`` can supply this port.

        Types must be equal — no subtyping, no conversion (Invariant 59).
        Keys must agree *when both name one*; a keyless port is a wildcard,
        which is precisely why a keyless consumer facing two producers is
        ambiguous rather than resolved (spec §10).
        """
        if self.type != other.type:
            return False
        if self.key is not None and other.key is not None:
            return self.key == other.key
        return True

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.type}:{self.key}" if self.key else self.type


def parse_ports(specs) -> list[Port]:
    """Parse a capability's declared type list into ports."""
    return [Port.parse(spec) for spec in (specs or ())]


@dataclass
class TypedOutput:
    """A process result labelled with what *kind* of thing it is.

    A result has to say what it is before anything downstream can consume it.
    Kept additive: a handler returning a plain dict still works and simply
    contributes nothing to data flow.  ``key`` distinguishes two results of the
    same type from one process.
    """

    type: str
    value: Any = None
    key: str | None = None

    @property
    def port(self) -> Port:
        """This output as a port."""
        return Port(self.type, self.key)

    def to_dict(self) -> dict:
        data = {"type": self.type, "value": self.value}
        if self.key is not None:
            data["key"] = self.key
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "TypedOutput":
        return cls(
            type=data.get("type", ""),
            value=data.get("value"),
            key=data.get("key"),
        )


def typed_outputs_of(output: Any) -> list[TypedOutput]:
    """Extract the typed outputs from a process's ``output`` dict.

    Recognises ``{"outputs": [{"type": ..., "value": ...}]}`` and the
    single-output shorthand ``{"output_type": ..., "value": ...}``.  Anything
    else has no declared type and simply contributes nothing to data flow —
    which is exactly right for the many processes that predate this phase.
    """
    if not isinstance(output, dict):
        return []
    raw = output.get("outputs")
    if isinstance(raw, list):
        return [TypedOutput.from_dict(item) for item in raw if isinstance(item, dict)]
    if isinstance(output.get("output_type"), str):
        return [TypedOutput(type=output["output_type"], value=output.get("value"))]
    return []


@dataclass
class ProcessPlan:
    """A durable, ordered way to satisfy one WorkRequirement with several processes.

    Attributes:
        planning_snapshot: What the planner was looking at — the definitions and
            capability versions it chose from (spec §65).  Not the whole
            registry: only what the decision actually rested on, so the record
            explains the choice without archiving the world.
        input_types: What was available to start from.
        required_output_types: What the plan must produce to count as done.
    """

    work_requirement_id: uuid.UUID
    status: PlanStatus = PlanStatus.PROPOSED
    planner_name: str = "composition"
    planner_version: str = "1"
    required_capabilities: list = field(default_factory=list)
    input_types: list[str] = field(default_factory=list)
    required_output_types: list[str] = field(default_factory=list)
    planning_snapshot: dict = field(default_factory=dict)
    created_by_process_id: uuid.UUID | None = None
    reasons: list[str] = field(default_factory=list)
    #: Phase 4C: the identity of this plan's *shape* (see ``planning.fingerprint``).
    #: A plan is one candidate among several now, so it needs a way to be
    #: compared with, and distinguished from, plans it is not.
    fingerprint: str | None = None
    #: Which replanning round produced this plan; 0 for the first attempt.
    replan_attempt: int = 0
    #: The failed plan this one was composed to replace, if any.  Recorded
    #: rather than mutating the old plan (Invariant 80).
    supersedes_plan_id: uuid.UUID | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None


@dataclass
class PlanNode:
    """One position in a plan: which definition, for which capabilities.

    ``node_key`` is the node's logical identity within its plan.  Together with
    the plan id it is what makes execution idempotent (spec §51): a restart
    looks up ``(plan_id, node_key)`` and re-attaches to the instance already
    created for it rather than spawning a second.
    """

    plan_id: uuid.UUID
    node_key: str
    definition_name: str
    definition_version: str
    provided_capabilities: list[str] = field(default_factory=list)
    input_types: list[str] = field(default_factory=list)
    output_types: list[str] = field(default_factory=list)
    status: PlanNodeStatus = PlanNodeStatus.PENDING
    process_instance_id: uuid.UUID | None = None
    depth: int = 0
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def key(self) -> tuple[str, str]:
        """The ``(definition_name, definition_version)`` this node uses."""
        return (self.definition_name, self.definition_version)


@dataclass
class PlanEdge:
    """An explicit binding: *this* output of one node feeds *that* input of another.

    Phase 4B treated an edge as a dependency and let the executor pick a value
    by type at run time — which meant that when two nodes produced the same
    type, what got consumed depended on iteration order.  The plan could not
    explain its own data flow.

    Since Phase 4B.1 the edge **is** the data flow (Invariant 66): it names the
    producing port and the consuming port, and nothing is resolved at execution
    time that was not decided at planning time.

    ``artifact_type`` is the Phase 4B field, kept so plans written then still
    read back.
    """

    plan_id: uuid.UUID
    from_node_id: uuid.UUID
    to_node_id: uuid.UUID
    output_type: str | None = None
    input_type: str | None = None
    output_key: str | None = None
    input_key: str | None = None
    artifact_type: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)

    @property
    def output_port(self) -> Port:
        """The producing port this edge draws from."""
        return Port(self.output_type or self.artifact_type or "", self.output_key)

    @property
    def input_port(self) -> Port:
        """The consuming port this edge feeds."""
        return Port(self.input_type or self.artifact_type or "", self.input_key)

    @property
    def is_bound(self) -> bool:
        """Whether this edge carries explicit binding information.

        A Phase 4B edge does not; it is treated as a legacy dependency and is
        only usable when the binding it implies is unambiguous (spec §58).
        """
        return bool(self.output_type and self.input_type)

    def describe(self) -> str:
        """A short ``out -> in`` description, for traces and reasons."""
        return f"{self.output_port} -> {self.input_port}"


@dataclass
class PlanCandidate:
    """A plan the planner considered, valid or not.

    Candidates are data, not commitments: the planner produces them and does
    nothing else (spec §132).  Validation and selection happen afterwards.
    """

    nodes: list[PlanNode] = field(default_factory=list)
    edges: list[PlanEdge] = field(default_factory=list)
    covered_capabilities: list[str] = field(default_factory=list)
    missing_capabilities: list[str] = field(default_factory=list)
    produced_output_types: list[str] = field(default_factory=list)
    missing_output_types: list[str] = field(default_factory=list)
    #: Inputs the planner could not bind because several nodes offered the
    #: type and nothing said which.  Carried rather than discarded so the
    #: refusal can explain itself (spec §11): each entry is
    #: ``{"node": node_key, "port": "type[:key]", "sources": [node_key, ...]}``.
    ambiguous_bindings: list[dict] = field(default_factory=list)
    valid: bool = False
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def node_count(self) -> int:
        """How many processes this plan would run."""
        return len(self.nodes)

    @property
    def depth(self) -> int:
        """The longest chain in the plan (1 for a single stage)."""
        return max((node.depth for node in self.nodes), default=-1) + 1

    def to_dict(self) -> dict:
        """A compact record of this candidate, for the planning audit."""
        return {
            "nodes": [
                {
                    "node_key": n.node_key,
                    "definition": f"{n.definition_name}:v{n.definition_version}",
                    "provides": list(n.provided_capabilities),
                    "inputs": list(n.input_types),
                    "outputs": list(n.output_types),
                    "depth": n.depth,
                }
                for n in self.nodes
            ],
            "edges": [
                {
                    "from": str(e.from_node_id),
                    "to": str(e.to_node_id),
                    "type": e.artifact_type,
                    "binding": e.describe(),
                }
                for e in self.edges
            ],
            "covered": list(self.covered_capabilities),
            "missing": list(self.missing_capabilities),
            "ambiguous_bindings": [dict(a) for a in self.ambiguous_bindings],
            "produced_outputs": list(self.produced_output_types),
            "missing_outputs": list(self.missing_output_types),
            "node_count": self.node_count,
            "depth": self.depth,
            "valid": self.valid,
            "score": self.score,
            "reasons": list(self.reasons),
        }


class BindingStatus(str, Enum):
    """Why one consumer input is or is not satisfiable (spec §12)."""

    BOUND = "BOUND"
    MISSING_INPUT = "MISSING_INPUT"
    AMBIGUOUS_BINDING = "AMBIGUOUS_BINDING"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    DUPLICATE_BINDING = "DUPLICATE_BINDING"
    INVALID_PORT = "INVALID_PORT"


@dataclass
class PlanValidation:
    """Why a candidate plan is or is not fit to run."""

    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    binding_issues: list[BindingStatus] = field(default_factory=list)

    def fail(self, reason: str, status: BindingStatus | None = None) -> None:
        """Record a reason this plan cannot be run."""
        self.ok = False
        self.reasons.append(reason)
        if status is not None:
            self.binding_issues.append(status)

    def has(self, status: BindingStatus) -> bool:
        """Whether a particular binding problem was found."""
        return status in self.binding_issues


@dataclass
class SearchBounds:
    """Limits that keep composition search finite (spec §22–§23).

    A planner that can loop is a planner that can hang the runtime.  These are
    configuration, not constants, but they always exist.
    """

    max_plan_nodes: int = 10
    max_search_depth: int = 6
    max_candidate_plans: int = 20
