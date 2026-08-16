"""Proposal validation: schema and current-state consistency."""

from __future__ import annotations

from nexus_seed.intelligence.proposal import InterpretationProposal, ProposedStateDelta
from nexus_seed.intelligence.validation import validate_proposal


def _proposal(*, subject="D1_CD", confidence=0.9, deltas=None):
    return InterpretationProposal(
        subject=subject,
        predicate="target_changed",
        proposed_state_deltas=deltas
        if deltas is not None
        else [ProposedStateDelta("D1_CD", "target", 48, 45, "nm", 0.95)],
        confidence=confidence,
    )


def _lookup(state):
    return lambda e, a: state.get(e, {}).get(a)


def test_valid_proposal():
    result = validate_proposal(_proposal(), current_value=_lookup({"D1_CD": {"target": 48}}))
    assert result.schema_ok and result.consistency_ok


def test_valid_when_attribute_absent():
    # old_value present but nothing stored yet -> creation, no conflict.
    result = validate_proposal(_proposal(), current_value=_lookup({}))
    assert result.schema_ok and result.consistency_ok


def test_schema_fail_empty_subject():
    result = validate_proposal(_proposal(subject=""), current_value=_lookup({}))
    assert result.schema_ok is False


def test_schema_fail_confidence_out_of_range():
    result = validate_proposal(_proposal(confidence=1.4), current_value=_lookup({}))
    assert result.schema_ok is False


def test_schema_fail_no_deltas():
    result = validate_proposal(_proposal(deltas=[]), current_value=_lookup({}))
    assert result.schema_ok is False


def test_explicit_empty_state_delta_array_is_valid():
    proposal = InterpretationProposal.from_output(
        {
            "subject": "D1_CD",
            "predicate": "unchanged",
            "confidence": 0.95,
            "rationale": "No durable fact changed.",
            "proposed_state_deltas": [],
        }
    )

    assert proposal is not None
    result = validate_proposal(proposal, current_value=_lookup({}))
    assert result.schema_ok is True
    assert result.consistency_ok is True


def test_missing_state_delta_field_is_still_invalid():
    proposal = InterpretationProposal.from_output(
        {"subject": "D1_CD", "predicate": "unchanged", "confidence": 0.95}
    )

    assert proposal is not None
    result = validate_proposal(proposal, current_value=_lookup({}))
    assert result.schema_ok is False
    assert "missing or invalid required field proposed_state_deltas" in result.reasons


def test_consistency_fail_on_old_value_mismatch():
    result = validate_proposal(
        _proposal(deltas=[ProposedStateDelta("D1_CD", "target", 50, 45, "nm", 0.9)]),
        current_value=_lookup({"D1_CD": {"target": 48}}),
    )
    assert result.schema_ok is True
    assert result.consistency_ok is False
