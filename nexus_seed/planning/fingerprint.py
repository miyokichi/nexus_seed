"""Plan structural fingerprint — the identity of a plan's *shape*.

Phase 4C compares plans instead of taking the first valid one, and the first
thing comparison needs is a way to tell two plans apart that does not consult
their ids.  Every candidate has fresh UUIDs, so by identity all candidates are
distinct; by *structure* many are the same plan enumerated twice.

The fingerprint covers what actually determines behaviour (spec §7):

    the definitions used
    the edges between them
    the ports those edges bind

and deliberately excludes plan id, node ids, creation times and the order the
search happened to produce.  Two plans with the same fingerprint would do the
same thing, so offering both as a choice would be a choice about nothing.
"""

from __future__ import annotations

import hashlib


def node_signature(node) -> str:
    """One node, as the part of it that affects behaviour."""
    capabilities = ",".join(sorted(node.provided_capabilities or ()))
    return f"{node.definition_name}:v{node.definition_version}[{capabilities}]"


def edge_signature(edge, node_keys: dict) -> str:
    """One binding, in terms of node keys rather than node ids."""
    producer = node_keys.get(edge.from_node_id, "?")
    consumer = node_keys.get(edge.to_node_id, "?")
    return f"{producer}.{edge.output_port}->{consumer}.{edge.input_port}"


def plan_fingerprint(nodes, edges) -> str:
    """A deterministic hash of a plan's structure.

    Stable across processes and restarts: it is built from sorted text, not
    from any hash whose seed varies per interpreter run.
    """
    keys = {n.id: n.node_key for n in nodes}
    node_part = ";".join(sorted(node_signature(n) for n in nodes))
    edge_part = ";".join(sorted(edge_signature(e, keys) for e in (edges or ())))
    payload = f"{node_part}||{edge_part}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def candidate_fingerprint(candidate) -> str:
    """The fingerprint of a :class:`PlanCandidate`."""
    return plan_fingerprint(candidate.nodes, candidate.edges)


def dedupe_by_fingerprint(candidates) -> list:
    """Keep the first candidate of each distinct shape, in the given order.

    Order-preserving on purpose: the caller has already ranked these, and the
    best-ranked instance of a shape is the one worth keeping.
    """
    seen: set[str] = set()
    unique = []
    for candidate in candidates:
        fingerprint = candidate_fingerprint(candidate)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(candidate)
    return unique


__all__ = [
    "candidate_fingerprint",
    "dedupe_by_fingerprint",
    "edge_signature",
    "node_signature",
    "plan_fingerprint",
]
