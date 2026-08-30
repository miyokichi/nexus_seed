"""Operator CLI coverage for the file-backed Semantica boundary."""

from __future__ import annotations

import json
from pathlib import Path

from nexus_seed.modules.knowledge import SemanticaKnowledgeAdapter
from nexus_seed.semantica_cli import main


ROOT = Path(__file__).parents[1]
SAMPLE = ROOT / "modules" / "knowledge" / "samples" / "assumption_v0.1.yaml"


class CanonicalRuntime:
    """Keep CLI configuration coverage independent of the optional extra."""

    def build(self, source, *, content, infer_relations, relation_types):
        return {
            "entities": list(source["entities"]),
            "relationships": list(source["relationships"]),
            "metadata": {},
        }


def test_query_loads_snapshot_path_from_env_file(tmp_path, monkeypatch, capsys) -> None:
    snapshot = tmp_path / "semantic.json"
    SemanticaKnowledgeAdapter(snapshot, runtime=CanonicalRuntime()).ingest(SAMPLE)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"NEXUS_SEED_SEMANTICA_SNAPSHOT={snapshot}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("NEXUS_SEED_SEMANTICA_SNAPSHOT", raising=False)

    exit_code = main(["--env-file", str(env_file), "query", "WL Width"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["properties"]["wl_width"]["typical"] == 50
    assert payload["relations"]["explicit"][0]["type"] == "uses_parameter"
