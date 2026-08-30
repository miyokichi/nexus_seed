"""What the loop learned has to be visible, or it cannot be trusted.

Consolidation and principle extraction run automatically now, so a person has
to be able to see what was concluded — and on what evidence — in both the
Cockpit and the CLI. These tests cover that surface, and the CLI's ability to
act on what the loop is waiting for, which previously only the Cockpit could.
"""

from __future__ import annotations

import json

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.cockpit import CockpitService
from nexus_seed.knowledge.autonomous_loop import (
    KIND_PROJECT_PROPOSAL,
    PROPOSAL_PENDING_REVIEW,
    KnowledgeLoop,
)
from nexus_seed.knowledge.models import (
    KIND_CONSOLIDATED_MEMORY,
    KIND_PRINCIPLE,
    STATUS_SUPPORTED,
)
from nexus_seed.knowledge_cli import main
from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator
from nexus_seed.runtime.runtime import Runtime


def build(tmp_path, backend=None, **kwargs):
    runtime = Runtime(tmp_path / "loop.db")
    orchestrator = ProjectOrchestrator(
        runtime.db, agent_runtime=InProcessAgentRuntime()
    )
    loop = KnowledgeLoop(runtime, orchestrator, backend=backend, **kwargs)
    runtime.knowledge_loop = loop
    return runtime, loop


def _run(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- Cockpit -----------------------------------------------------------------


async def test_cockpit_shows_principles_with_their_evidence(tmp_path):
    runtime, loop = build(tmp_path)
    loop.ledger.record(
        "不確実な状態での不可逆な決定は手戻りを増やす",
        source_type="principle_extraction",
        kind=KIND_PRINCIPLE,
        status=STATUS_SUPPORTED,
        derived_from=["CM-1", "CM-2"],
        metadata={
            "scope": "仕様凍結",
            "support_count": 3,
            "counterexample_count": 1,
            "evidence_for": ["CM-1", "CM-2"],
            "evidence_against": ["CM-9"],
        },
    )

    knowledge = CockpitService(runtime, master_id="operator").snapshot()["knowledge"]

    [principle] = knowledge["principles"]
    assert principle["status"] == STATUS_SUPPORTED
    assert principle["support_count"] == 3
    assert principle["counterexample_count"] == 1
    assert principle["scope"] == "仕様凍結"
    assert principle["evidence_against"] == ["CM-9"]
    runtime.close()


async def test_cockpit_shows_a_candidate_principle_too(tmp_path):
    """A person judging the loop must see what it proposes to learn."""
    runtime, loop = build(tmp_path)
    loop.ledger.record(
        "まだ検証されていない仮の原則",
        source_type="principle_extraction",
        kind=KIND_PRINCIPLE,
        status="candidate",
    )

    knowledge = CockpitService(runtime, master_id="operator").snapshot()["knowledge"]
    assert [p["status"] for p in knowledge["principles"]] == ["candidate"]
    runtime.close()


async def test_cockpit_shows_consolidated_memories(tmp_path):
    runtime, loop = build(tmp_path)
    loop.ledger.record(
        "契約Aについての経緯をまとめた記憶",
        source_type="consolidation",
        kind=KIND_CONSOLIDATED_MEMORY,
        derived_from=["K-1", "K-2", "K-3"],
        metadata={"about": "contract-A", "generation": 1, "unresolved": ["更新可否"]},
    )

    knowledge = CockpitService(runtime, master_id="operator").snapshot()["knowledge"]

    [memory] = knowledge["memories"]
    assert memory["derived_from"] == ["K-1", "K-2", "K-3"]
    assert memory["metadata"]["about"] == "contract-A"
    assert memory["metadata"]["unresolved"] == ["更新可否"]
    runtime.close()


async def test_derived_knowledge_is_not_mixed_into_the_inbox(tmp_path):
    """The inbox is what came in, not what was concluded from it."""
    runtime, loop = build(tmp_path)
    loop.ledger.record("外から届いた観測", source_type="meeting")
    loop.ledger.record("記憶", source_type="consolidation", kind=KIND_CONSOLIDATED_MEMORY)
    loop.ledger.record("原則", source_type="principle_extraction", kind=KIND_PRINCIPLE)

    knowledge = CockpitService(runtime, master_id="operator").snapshot()["knowledge"]

    assert [item["kind"] for item in knowledge["inbox"]] == ["raw"]
    assert len(knowledge["principles"]) == 1
    assert len(knowledge["memories"]) == 1
    runtime.close()


async def test_cockpit_without_a_loop_still_answers(tmp_path):
    runtime = Runtime(tmp_path / "none.db")
    knowledge = CockpitService(runtime, master_id="operator").snapshot()["knowledge"]
    assert knowledge["enabled"] is False
    assert knowledge["principles"] == []
    assert knowledge["memories"] == []
    runtime.close()


async def test_the_cockpit_page_renders_both_new_sections():
    from nexus_seed.cockpit.assets import APP_JS

    assert "knowledgePrinciple" in APP_JS
    assert "knowledgeMemory" in APP_JS
    assert "Consolidated Memory" in APP_JS


# --- CLI ----------------------------------------------------------------------


def test_cli_lists_principles_with_support_and_counterexamples(tmp_path, capsys):
    db = tmp_path / "k.db"
    _run(["record", "--db", str(db), "--content", "a", "--source-type", "experience"], capsys)
    _run(["record", "--db", str(db), "--content", "b", "--source-type", "experience"], capsys)
    ids = [row["knowledge_id"] for row in json.loads(_run(["list", "--db", str(db), "--json"], capsys)[1])]
    code, out, _ = _run(
        ["extract-principle", "--db", str(db), "--ids", *ids, "--no-llm", "--json"], capsys
    )
    pid = json.loads(out)["knowledge_id"]
    _run(["support", "--db", str(db), "--principle-id", pid, "--evidence-id", "e1"], capsys)

    code, out, _ = _run(["principles", "--db", str(db)], capsys)
    assert code == 0
    assert pid in out
    assert "支持 1 / 反例 0" in out

    # A candidate is listed by default and hidden behind --mature-only.
    code, out, _ = _run(["principles", "--db", str(db), "--mature-only"], capsys)
    assert "(0 principles)" in out


def test_cli_lists_memories_with_what_they_compressed(tmp_path, capsys):
    db = tmp_path / "k.db"
    for text in ("note 1", "note 2"):
        _run(["record", "--db", str(db), "--content", text, "--source-type", "meeting",
              "--about", "project-A"], capsys)
    _run(["consolidate", "--db", str(db), "--about", "project-A", "--no-llm"], capsys)

    code, out, _ = _run(["memories", "--db", str(db)], capsys)
    assert code == 0
    assert "about=project-A" in out
    assert "2 sources" in out
    assert "(1 memories)" in out


def test_cli_reconcile_reports_a_quiet_pass(tmp_path, capsys):
    db = tmp_path / "k.db"
    code, out, _ = _run(["reconcile", "--db", str(db), "--no-llm"], capsys)
    assert code == 0
    assert "nothing changed" in out


def test_cli_pending_and_decide_drive_the_same_loop_the_cockpit_does(tmp_path, capsys):
    db = tmp_path / "k.db"
    runtime, loop = build(tmp_path)
    # A proposal waiting on a person, as the evaluator would have left it.
    loop.ledger.record(
        "契約Aの更新可否を確認する",
        source_type="situation_evaluator",
        kind=KIND_PROJECT_PROPOSAL,
        status=PROPOSAL_PENDING_REVIEW,
        derived_from=["K-1"],
        metadata={"objective": "契約Aの更新可否を確認する", "evidence_ids": ["K-1"]},
    )
    proposal_id = loop.proposals()[0].knowledge_id
    runtime.close()

    loop_db = str(tmp_path / "loop.db")
    code, out, _ = _run(["pending", "--db", loop_db, "--no-llm"], capsys)
    assert code == 0
    assert proposal_id in out
    assert "(1 waiting for a decision)" in out

    code, out, _ = _run(["decide", "--db", loop_db, proposal_id, "reject",
                         "--note", "今期は対象外", "--no-llm"], capsys)
    assert code == 0
    assert "REJECTED" in out

    code, out, _ = _run(["pending", "--db", loop_db, "--no-llm"], capsys)
    assert "(0 waiting for a decision)" in out


def test_cli_decide_refuses_a_kind_that_takes_no_decision(tmp_path, capsys):
    db = str(tmp_path / "loop.db")
    runtime, loop = build(tmp_path)
    item = loop.ledger.record("ただの観測", source_type="meeting")
    runtime.close()

    code, out, err = _run(["decide", "--db", db, item.knowledge_id, "approve", "--no-llm"], capsys)
    assert code == 1
    assert "does not take an approve/reject decision" in err


def test_cli_decide_reports_an_unknown_id_cleanly(tmp_path, capsys):
    code, out, err = _run(
        ["decide", "--db", str(tmp_path / "k.db"), "K-nope", "approve", "--no-llm"], capsys
    )
    assert code == 1
    assert "unknown knowledge_id" in err
    assert "Traceback" not in err
