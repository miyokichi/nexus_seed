"""Command-line entry point for one complete MVP loop."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from ..backends.llm import LLMBackend
from ..llm_config import LLMConfigurationError, LLMSettings
from ..knowledge import KnowledgeLedger
from ..orchestrator.project_manager import ProjectManager as ExistingProjectManager
from ..runtime import Runtime as DurableRuntime
from ..storage import KnowledgeStore
from ..storage.orchestrator_store import ProjectStore
from .approval import CLIHumanApproval, FixedApproval
from .executor import DefaultProjectExecutor
from .knowledge import ExistingKnowledgeGateway
from .llm import ExistingBackendLLMProvider
from .observer import ManualIngressObserver
from .planner import SimpleProjectPlanner
from .project import ExistingProjectManagerAdapter
from .runtime import MVPApplicationRuntime
from .testing import MockLLMProvider


def build_parser() -> argparse.ArgumentParser:
    """Build the small MVP CLI parser."""

    parser = argparse.ArgumentParser(
        prog="nexus-seed-mvp",
        description="Run one Observer -> Knowledge -> Project -> Result loop.",
    )
    parser.add_argument("input", help="external or human text to observe")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("NEXUS_SEED_DATA_DIR", "~/.nexus_seed"),
        help="directory containing the durable SQLite database",
    )
    parser.add_argument("--workspace", default=".", help="read-only workspace for execution")
    parser.add_argument(
        "--source-key",
        default=None,
        help="stable external id for ingress deduplication (generated when omitted)",
    )
    parser.add_argument("--env-file", default=".env", help="LLM dotenv file")
    parser.add_argument("--yes", action="store_true", help="approve every displayed proposal")
    parser.add_argument(
        "--mock-llm",
        action="store_true",
        help="use a deterministic offline result (demo/testing only)",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one durable MVP pass and return a process exit code."""

    args = build_parser().parse_args(argv)
    try:
        provider = _provider(args)
    except LLMConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    durable_runtime = DurableRuntime(data_dir / "nexus_seed.db")
    db = durable_runtime.db
    try:
        knowledge = ExistingKnowledgeGateway(KnowledgeLedger(KnowledgeStore(db)))
        manager = ExistingProjectManagerAdapter(
            ExistingProjectManager(ProjectStore(db))
        )
        runtime = MVPApplicationRuntime(
            observer=ManualIngressObserver(
                durable_runtime.ingress,
                args.input,
                source_event_key=args.source_key or f"mvp-cli-{uuid.uuid4()}",
                metadata={"surface": "nexus-seed-mvp"},
            ),
            knowledge=knowledge,
            planner=SimpleProjectPlanner(),
            approval=FixedApproval(True) if args.yes else CLIHumanApproval(),
            project_manager=manager,
            executor=DefaultProjectExecutor(provider),
            workspace=str(Path(args.workspace).expanduser().resolve()),
        )
        report = runtime.run()
    finally:
        durable_runtime.close()

    payload = {
        "observations": len(report.observations),
        "proposals": len(report.proposals),
        "rejected": len(report.rejected_proposal_ids),
        "projects": [
            {
                "id": project.id,
                "title": project.title,
                "status": project.status.value,
                "summary": project.result.summary if project.result else None,
            }
            for project in report.projects
        ],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    elif report.projects:
        for project in report.projects:
            print(f"{project.status.value}: {project.title}")
            if project.result:
                print(project.result.summary)
    elif report.rejected_proposal_ids:
        print("Proposal rejected; no Project was created.")
    else:
        print("No Project was proposed.")
    return 1 if any(project.status.value == "FAILED" for project in report.projects) else 0


def _provider(args) -> ExistingBackendLLMProvider | MockLLMProvider:
    if args.mock_llm:
        return MockLLMProvider(
            default={
                "summary": "READMEの改善案を作成しました",
                "outputs": {
                    "suggestions": [
                        "目的と対象読者を冒頭で明確にする",
                        "最短の実行例と期待結果を並べる",
                    ]
                },
            }
        )
    settings = LLMSettings.from_env(args.env_file)
    if not settings.enabled:
        raise LLMConfigurationError(
            "MVP execution needs an LLM; enable NEXUS_SEED_LLM_ENABLED or use --mock-llm"
        )
    backend = LLMBackend(
        provider=settings.provider,
        model=settings.model,
        api_key_env=settings.api_key_env,
        max_tokens=settings.max_tokens,
        base_url=settings.base_url,
        timeout_seconds=settings.timeout_seconds,
    )
    return ExistingBackendLLMProvider(backend)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
