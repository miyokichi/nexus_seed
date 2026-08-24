"""nexus_seed.knowledge_cli — the CLI wraps the same Python API tested in
tests/test_knowledge_*.py, so these tests focus on argument wiring, JSON
output, and clean (non-traceback) error handling rather than re-testing
domain behaviour already covered elsewhere.
"""

from __future__ import annotations

import json

import pytest

from nexus_seed.knowledge_cli import main


def _run(argv, capsys):
    """Run the CLI and return (exit_code, stdout, stderr)."""
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _record(db, capsys, content, *, source_type="meeting", about=None, source_ref=None):
    argv = ["record", "--db", str(db), "--content", content, "--source-type", source_type, "--json"]
    if about:
        argv += ["--about", about]
    if source_ref:
        argv += ["--source-ref", source_ref]
    code, out, err = _run(argv, capsys)
    assert code == 0, err
    return json.loads(out)["knowledge_id"]


# --- K1: record / revise / relate / fact / show / list ----------------------


def test_record_and_show(tmp_path, capsys):
    db = tmp_path / "k.db"
    kid = _record(db, capsys, "B案の方がmarginはありそう", about="project-A")

    code, out, _ = _run(["show", "--db", str(db), kid], capsys)
    assert code == 0
    assert "B案の方がmarginはありそう" in out
    assert "relation:      about -> project-A" in out


def test_record_from_stdin(tmp_path, capsys, monkeypatch):
    import io

    db = tmp_path / "k.db"
    monkeypatch.setattr("sys.stdin", io.StringIO("piped content"))
    code, out, err = _run(
        ["record", "--db", str(db), "--source-type", "chat", "--json"], capsys
    )
    assert code == 0, err
    assert json.loads(out)["content"]["value"] == "piped content"


def test_revise_keeps_history(tmp_path, capsys):
    db = tmp_path / "k.db"
    kid = _record(db, capsys, "v1 content")

    code, _, _ = _run(["revise", "--db", str(db), kid, "--content", "v2 content"], capsys)
    assert code == 0

    code, out, _ = _run(["show", "--db", str(db), kid, "--history"], capsys)
    assert code == 0
    assert "v1  [raw" in out
    assert "v2  [raw" in out


def test_relate_and_conflict(tmp_path, capsys):
    db = tmp_path / "k.db"
    a = _record(db, capsys, "deadline: Sep 1", source_type="email")
    b = _record(db, capsys, "deadline: Sep 15", source_type="email")

    code, _, _ = _run(["relate", "--db", str(db), a, "--type", "supports", "--target", b], capsys)
    assert code == 0

    code, out, _ = _run(["conflict", "--db", str(db), a, b, "--reason", "disagree"], capsys)
    assert code == 0
    assert "marked CONFLICT" in out

    code, out, _ = _run(["show", "--db", str(db), a, "--json"], capsys)
    assert json.loads(out)["status"] == "CONFLICT"


def test_fact_and_view(tmp_path, capsys):
    db = tmp_path / "k.db"
    kid = _record(db, capsys, "margin is insufficient")

    code, _, _ = _run(
        ["fact", "--db", str(db), kid, "--entity", "project-A", "--attribute", "margin", "--value", "insufficient"],
        capsys,
    )
    assert code == 0

    code, out, _ = _run(["view", "--db", str(db), "--json"], capsys)
    assert code == 0
    assert json.loads(out)["facts"]["project-A.margin"] == "insufficient"


def test_fact_value_is_json_when_it_parses(tmp_path, capsys):
    db = tmp_path / "k.db"
    kid = _record(db, capsys, "deadline pressure")
    _run(["fact", "--db", str(db), kid, "--entity", "project-A", "--attribute", "days_left", "--value", "3"], capsys)

    code, out, _ = _run(["view", "--db", str(db), "--json"], capsys)
    assert json.loads(out)["facts"]["project-A.days_left"] == 3  # int, not "3"


def test_list_filters_by_about_and_kind(tmp_path, capsys):
    db = tmp_path / "k.db"
    _record(db, capsys, "in scope", about="project-A")
    _record(db, capsys, "out of scope", about="project-B")

    code, out, _ = _run(["list", "--db", str(db), "--about", "project-A", "--json"], capsys)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["content"]["value"] == "in scope"


def test_diff_emits_events(tmp_path, capsys):
    import datetime

    db = tmp_path / "k.db"
    before = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat()
    kid = _record(db, capsys, "new fact")
    _run(["fact", "--db", str(db), kid, "--entity", "project-A", "--attribute", "status", "--value", "changed"], capsys)

    code, out, _ = _run(["diff", "--db", str(db), "--before", before, "--emit-events"], capsys)
    assert code == 0
    assert "added" in out
    assert "emitted 1 state_changed event" in out

    from nexus_seed.storage import Database, EventStore

    events = EventStore(Database(db)).by_type("state_changed")
    assert len(events) == 1
    assert events[0].payload["entity"] == "project-A"


# --- K3/K4: consolidate / extract-principle / support / refine (no LLM) -----


def test_consolidate_no_llm_never_fabricates(tmp_path, capsys):
    db = tmp_path / "k.db"
    _record(db, capsys, "note 1", about="project-A")
    _record(db, capsys, "note 2", about="project-A")

    code, out, _ = _run(
        ["consolidate", "--db", str(db), "--about", "project-A", "--no-llm", "--json"], capsys
    )
    assert code == 0
    data = json.loads(out)
    assert data["kind"] == "consolidated_memory"
    assert data["metadata"]["confidence"] == 0.0
    assert len(data["derived_from"]) == 2


def test_consolidate_needs_at_least_two_candidates(tmp_path, capsys):
    db = tmp_path / "k.db"
    _record(db, capsys, "lonely note", about="project-A")

    code, out, err = _run(
        ["consolidate", "--db", str(db), "--about", "project-A", "--no-llm"], capsys
    )
    assert code == 1
    assert "only 1 candidate" in err


def test_extract_principle_then_support_promotes_status(tmp_path, capsys):
    db = tmp_path / "k.db"
    _record(db, capsys, "x -> y", source_type="experience", about="topic")
    _record(db, capsys, "x -> y", source_type="experience", about="topic")

    code, out, _ = _run(
        ["extract-principle", "--db", str(db), "--about", "topic", "--no-llm", "--json"], capsys
    )
    assert code == 0
    pid = json.loads(out)["knowledge_id"]
    assert json.loads(out)["status"] == "candidate"

    _run(["support", "--db", str(db), "--principle-id", pid, "--evidence-id", "e1"], capsys)
    code, out, _ = _run(["support", "--db", str(db), "--principle-id", pid, "--evidence-id", "e2"], capsys)
    assert '"status":        supported' not in out  # (plain-text output, not json)
    assert "status:        supported" in out


def test_refine_narrows_scope_and_demotes(tmp_path, capsys):
    db = tmp_path / "k.db"
    _record(db, capsys, "x -> y", source_type="experience")
    _record(db, capsys, "x -> y", source_type="experience")
    code, out, _ = _run(
        ["extract-principle", "--db", str(db), "--ids"]
        + [
            json.loads(_run(["list", "--db", str(db), "--json"], capsys)[1])[i]["knowledge_id"]
            for i in range(2)
        ]
        + ["--no-llm", "--json"],
        capsys,
    )
    pid = json.loads(out)["knowledge_id"]
    _run(["support", "--db", str(db), "--principle-id", pid, "--evidence-id", "e1"], capsys)
    _run(["support", "--db", str(db), "--principle-id", pid, "--evidence-id", "e2"], capsys)

    counter = _record(db, capsys, "x but not y", source_type="experience")
    code, out, _ = _run(
        ["refine", "--db", str(db), "--principle-id", pid, "--counterexample-ids", counter, "--json"],
        capsys,
    )
    assert code == 0
    data = json.loads(out)
    assert data["status"] == "refined"
    assert counter in data["metadata"]["evidence_against"]


# --- K4: predict / evaluate (structured, mechanical scoring) ----------------


def test_predict_without_llm_still_records_a_prediction(tmp_path, capsys):
    db = tmp_path / "k.db"
    _record(db, capsys, "x -> y", source_type="experience")
    _record(db, capsys, "x -> y", source_type="experience")
    ids = [row["knowledge_id"] for row in json.loads(_run(["list", "--db", str(db), "--json"], capsys)[1])]
    code, out, _ = _run(
        ["extract-principle", "--db", str(db), "--ids", *ids, "--no-llm", "--json"], capsys
    )
    pid = json.loads(out)["knowledge_id"]

    code, out, _ = _run(
        ["predict", "--db", str(db), "--principle-id", pid, "--subject", "project-Z", "--no-llm", "--json"],
        capsys,
    )
    assert code == 0
    assert json.loads(out)["kind"] == "prediction"


def test_evaluate_scores_a_structured_prediction_and_feeds_back(tmp_path, capsys):
    db = tmp_path / "k.db"
    # Build a principle the normal way, then hand-craft a prediction with a
    # structured predicted_state (as an LLM-backed `predict` would produce).
    a = _record(db, capsys, "x -> y", source_type="experience")
    b = _record(db, capsys, "x -> y", source_type="experience")
    code, out, _ = _run(
        ["extract-principle", "--db", str(db), "--ids", a, b, "--no-llm", "--json"], capsys
    )
    pid = json.loads(out)["knowledge_id"]

    from nexus_seed.knowledge.ledger import KnowledgeLedger
    from nexus_seed.knowledge.models import KIND_PREDICTION
    from nexus_seed.storage import Database, KnowledgeStore

    ledger = KnowledgeLedger(KnowledgeStore(Database(db)))
    prediction = ledger.record(
        "predicted rework risk increase",
        source_type="prediction",
        kind=KIND_PREDICTION,
        derived_from=[pid],
        metadata={"predicted_state": [{"entity": "project-Z", "attribute": "risk", "value": "high"}]},
    )

    code, out, _ = _run(
        [
            "evaluate", "--db", str(db),
            "--prediction-id", prediction.knowledge_id,
            "--actual", '{"project-Z": {"risk": "high"}}',
            "--principle-id", pid,
        ],
        capsys,
    )
    assert code == 0
    assert "matched: True" in out
    assert "principle after feedback" in out
    assert "support_count" in out


def test_evaluate_without_predicted_state_errors_cleanly(tmp_path, capsys):
    db = tmp_path / "k.db"
    kid = _record(db, capsys, "unstructured prediction", source_type="prediction")

    code, out, err = _run(
        ["evaluate", "--db", str(db), "--prediction-id", kid, "--actual", "{}"], capsys
    )
    assert code == 1
    assert "predicted_state" in err


# --- K5: signals / propose (no LLM -> safe, no fabricated risk) -------------


def test_signals_without_llm_finds_nothing(tmp_path, capsys):
    db = tmp_path / "k.db"
    code, out, _ = _run(["signals", "--db", str(db), "--no-llm"], capsys)
    assert code == 0
    assert "no signals detected" in out


def test_propose_without_llm_proposes_nothing(tmp_path, capsys):
    db = tmp_path / "k.db"
    code, out, _ = _run(["propose", "--db", str(db), "--no-llm"], capsys)
    assert code == 0
    assert "nothing proposed" in out


# --- K6: experience / advise -------------------------------------------------


def test_experience_and_advise(tmp_path, capsys):
    db = tmp_path / "k.db"
    code, out, _ = _run(
        [
            "experience", "--db", str(db),
            "--agent-id", "agent-A", "--task", "t", "--outcome", "failed then switched, succeeded",
            "--switches-json", '[{"from": "agent-A", "to": "agent-B", "reason": "same error"}]',
        ],
        capsys,
    )
    assert code == 0
    assert "agent-B" in out

    code, out, _ = _run(["advise", "--db", str(db)], capsys)
    assert code == 0
    assert "no mature" in out  # nothing supported/validated yet


# --- error handling ----------------------------------------------------------


def test_unknown_knowledge_id_is_a_clean_error_not_a_traceback(tmp_path, capsys):
    db = tmp_path / "k.db"
    code, out, err = _run(["show", "--db", str(db), "K-doesnotexist"], capsys)
    assert code == 1
    assert "no such knowledge_id" in err

    code, out, err = _run(
        ["support", "--db", str(db), "--principle-id", "K-doesnotexist", "--evidence-id", "e"], capsys
    )
    assert code != 0
    assert "Traceback" not in err
    assert "unknown knowledge_id" in err


def test_llm_and_no_llm_are_mutually_exclusive(tmp_path, capsys):
    db = tmp_path / "k.db"
    with pytest.raises(SystemExit):
        main(["consolidate", "--db", str(db), "--about", "x", "--llm", "--no-llm"])


def test_view_rejects_both_as_of_and_valid_at(tmp_path, capsys):
    db = tmp_path / "k.db"
    code, out, err = _run(
        ["view", "--db", str(db), "--as-of", "2026-01-01T00:00:00", "--valid-at", "2026-01-01T00:00:00"],
        capsys,
    )
    assert code == 1
    assert "at most one" in err


def test_diff_requires_exactly_one_before_kind(tmp_path, capsys):
    db = tmp_path / "k.db"
    code, out, err = _run(["diff", "--db", str(db)], capsys)
    assert code == 1
    assert "exactly one" in err
