"""Command-line ingress — hand NEXUS SEED one external occurrence by hand.

    python -m nexus_seed.ingress_cli --db world.db \
        --event-type human_message \
        --source-event-key demo-001 \
        --payload '{"text": "D1のCD targetを48nmから45nmへ変更しました"}'

Running the same command twice is a no-op: the second call reports
``duplicate`` and creates no second Event.  That is the same rule a webhook
redelivery obeys, made visible at the smallest possible scale.

The CLI registers no process definitions of its own — it only opens the
database, ingests, and reports.  Which processes react is decided by whoever
bootstrapped the runtime; ``--bootstrap`` opts into the standard perception +
work + action stack for demo use.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .adapters.manual import ManualAdapter
from .ingress.models import IngressStatus
from .ingress.service import IngressService
from .runtime.runtime import Runtime


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="nexus-seed-ingress",
        description="Submit one external occurrence through the ingress boundary.",
    )
    parser.add_argument("--db", required=True, help="path to the SQLite database")
    parser.add_argument("--adapter", default="manual", help="adapter id (default: manual)")
    parser.add_argument("--event-type", required=True, help="NEXUS Event type to raise")
    parser.add_argument(
        "--source-event-key",
        required=True,
        help="the outside world's identity for this occurrence (idempotency key)",
    )
    parser.add_argument("--payload", default="{}", help="event payload as JSON")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="register the standard perception/work/action processes before ingesting",
    )
    parser.add_argument(
        "--action-root",
        default=None,
        help="sandbox root for the file action backend (implies --bootstrap)",
    )
    return parser


def _bootstrap(runtime: Runtime, action_root: str | None) -> None:
    """Register the standard stack so an ingested event actually goes somewhere."""
    from .backends import FakeLLMBackend, LocalFileActionBackend
    from .processes.actions import bootstrap_actions
    from .processes.llm_interpret import bootstrap_llm_interpreter
    from .processes.semantic import bootstrap_semantic
    from .processes.work_intelligence import bootstrap_work_intelligence

    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)
    bootstrap_actions(runtime)
    bootstrap_llm_interpreter(runtime, FakeLLMBackend())
    if action_root:
        runtime.register_backend("local_file", LocalFileActionBackend(action_root))


async def run(args: argparse.Namespace) -> int:
    """Ingest one envelope; return the process exit code."""
    try:
        payload = json.loads(args.payload)
    except ValueError as exc:
        print(f"--payload is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("--payload must be a JSON object", file=sys.stderr)
        return 2

    runtime = Runtime(Path(args.db))
    try:
        if args.bootstrap or args.action_root:
            _bootstrap(runtime, args.action_root)

        adapter = ManualAdapter(args.adapter)
        envelope = adapter.envelope(
            event_type=args.event_type,
            source_event_key=args.source_event_key,
            payload=payload,
            metadata={"via": "ingress_cli"},
        )
        result = await IngressService(runtime).ingest(envelope)
    finally:
        runtime.close()

    print(json.dumps(_report(result), ensure_ascii=False, indent=2))
    return 0 if result.status is not IngressStatus.REJECTED else 1


def _report(result) -> dict:
    return {
        "status": result.status.value,
        "duplicate": result.duplicate,
        "receipt_id": str(result.receipt.id) if result.receipt else None,
        "event_id": str(result.event.id) if result.event else None,
        "reasons": result.reasons,
    }


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m nexus_seed.ingress_cli``."""
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
