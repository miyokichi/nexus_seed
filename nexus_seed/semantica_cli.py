"""Small operator CLI for the nexus_knowledge Semantica boundary."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .llm_config import load_env_file
from .modules.knowledge import (
    SemanticaKnowledgeAdapter,
    load_ontology_yaml,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the Canonical ingest/query command parser."""

    parser = argparse.ArgumentParser(prog="nexus-seed-semantica")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="dotenv file (default: .env)",
    )
    parser.add_argument(
        "--snapshot",
        default=os.environ.get("NEXUS_SEED_SEMANTICA_SNAPSHOT")
        or "semantica-knowledge.json",
        help="file-backed semantic graph snapshot",
    )
    parser.add_argument(
        "--ontology",
        default=os.environ.get("NEXUS_SEED_SEMANTICA_ONTOLOGY") or None,
        help="optional ontology YAML",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    ingest = subcommands.add_parser("ingest", help="ingest Canonical YAML v0.1")
    ingest.add_argument("yaml", help="Canonical YAML path")
    ingest.add_argument(
        "--infer-relations",
        action="store_true",
        help="ask Semantica for locally inferred relations",
    )

    query = subcommands.add_parser("query", help="query Entity/property/one-hop context")
    query.add_argument("text", help="Goal or Observation query")
    query.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Ingest or query through nexus_knowledge and return a process exit code."""

    env_parser = argparse.ArgumentParser(add_help=False)
    env_parser.add_argument("--env-file", default=".env")
    env_args, _ = env_parser.parse_known_args(argv)
    load_env_file(env_args.env_file)
    args = build_parser().parse_args(argv)
    ontology = load_ontology_yaml(args.ontology) if args.ontology else None
    adapter = SemanticaKnowledgeAdapter(
        Path(args.snapshot).expanduser().resolve(),
        ontology=ontology,
        infer_relations=bool(getattr(args, "infer_relations", False)),
    )
    if args.command == "ingest":
        graph = adapter.ingest(Path(args.yaml).expanduser().resolve())
        payload = {
            "entities": len(graph.get("entities", [])),
            "relations": len(graph.get("relationships", [])),
            "snapshot": str(adapter.snapshot_path),
        }
    else:
        context = adapter.query(args.text, limit=args.limit)
        payload = context.to_dict() if context is not None else None
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
