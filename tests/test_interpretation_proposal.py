"""InterpretationProposal model + store round-trip."""

from __future__ import annotations

from nexus_seed.intelligence.proposal import (
    InterpretationProposal,
    ProposalDecision,
    ProposedStateDelta,
)
from nexus_seed.intelligence.validation import validate_proposal
from nexus_seed.storage.database import Database
from nexus_seed.storage.proposal_store import ProposalStore


def test_from_output_parses_structured():
    proposal = InterpretationProposal.from_output(
        {
            "subject": "D1_CD",
            "predicate": "target_changed",
            "confidence": 0.9,
            "rationale": "nm change",
            "proposed_state_deltas": [
                {"entity": "D1_CD", "attribute": "target", "old_value": 48, "new_value": 45, "unit": "nm", "confidence": 0.95}
            ],
        }
    )
    assert proposal.subject == "D1_CD"
    assert proposal.confidence == 0.9
    assert proposal.proposed_state_deltas[0].new_value == 45
    assert proposal.proposed_state_deltas[0].unit == "nm"


def test_from_output_rejects_non_dict():
    assert InterpretationProposal.from_output(None) is None
    assert InterpretationProposal.from_output("not json") is None


def test_from_output_bad_confidence_is_out_of_range():
    proposal = InterpretationProposal.from_output(
        {"subject": "D1_CD", "confidence": "high", "proposed_state_deltas": []}
    )
    assert proposal.confidence == -1.0  # flagged as invalid by schema validation


def test_store_roundtrip_and_decision(tmp_path):
    store = ProposalStore(Database(tmp_path / "prop.db"))
    proposal = InterpretationProposal(
        subject="D1_CD",
        predicate="target_changed",
        proposed_state_deltas=[ProposedStateDelta("D1_CD", "target", 48, 45, "nm", 0.95)],
        confidence=0.9,
        rationale="why",
    )
    store.save(proposal)

    got = store.get(proposal.id)
    assert got is not None
    assert got.subject == "D1_CD"
    assert got.proposed_state_deltas[0].new_value == 45
    assert got.decision is ProposalDecision.PENDING

    store.update_decision(proposal.id, ProposalDecision.ACCEPT)
    assert store.get(proposal.id).decision is ProposalDecision.ACCEPT


def test_explicit_no_change_contract_survives_store_roundtrip(tmp_path):
    store = ProposalStore(Database(tmp_path / "no-change-prop.db"))
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
    store.save(proposal)

    restored = store.get(proposal.id)
    assert restored is not None
    assert restored.proposed_state_deltas == []
    validation = validate_proposal(restored, current_value=lambda _entity, _attribute: None)
    assert validation.schema_ok is True
