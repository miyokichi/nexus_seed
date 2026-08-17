"""Runnable NEXUS SEED application.

This module is deliberately application wiring. It composes the ordinary
Process pipelines, optional LLM backend, durable Runtime and webhook transport
without putting policy or domain knowledge into Runtime itself.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, replace
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any

from .adapters.webhook import WebhookIngress, WebhookServer
from .backends.action import LocalFileActionBackend
from .backends.base import BackendRequest
from .cockpit import CockpitService
from .federation_config import configure_external_agents
from .llm_config import LLMConfigurationError, configure_llm, load_env_file
from .operations import (
    OperationalCommandError,
    build_review_payload,
    read_pending_reviews,
    read_status,
    resolve_pending_review,
    submit_webhook_event,
    submit_control_command,
)
from .control.models import HumanIdentity
from .processes.autonomy import bootstrap_autonomy
from .orchestration import bootstrap_orchestration
from .processes.control import bootstrap_control
from .processes.extension import bootstrap_extension
from .processes.persistent_being import bootstrap_persistent_being
from .processes.planning import bootstrap_planning
from .processes.resources import bootstrap_observer, bootstrap_resources
from .processes.semantic import bootstrap_semantic
from .processes.work_intelligence import bootstrap_work_intelligence
from .resources.scope import ResourceScope
from .runtime.runtime import Runtime

logger = logging.getLogger(__name__)


class ApplicationConfigurationError(ValueError):
    """Raised when application settings cannot form a safe deployment."""


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Operational settings for the runnable application."""

    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8787
    webhook_token: str | None = None
    tick_seconds: float = 1.0
    log_level: str = "INFO"
    control_identity_id: str = "local-operator"
    control_permissions: tuple[str, ...] = ("command.*",)
    #: Phase 6 is on by default; False restores the complete Phase 5G wiring.
    phase6_enabled: bool = True
    #: Human Interface Layer; False removes every Cockpit HTTP route.
    cockpit_enabled: bool = True

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> AppSettings:
        """Load application settings from ``env_file`` and process environment."""

        load_env_file(env_file)
        default_data = Path.home() / ".nexus_seed"
        raw_data = os.environ.get("NEXUS_SEED_DATA_DIR", "").strip() or str(default_data)
        data_dir = Path(os.path.expandvars(raw_data)).expanduser().resolve()
        host = os.environ.get("NEXUS_SEED_WEBHOOK_HOST", "127.0.0.1").strip()
        port = _read_int("NEXUS_SEED_WEBHOOK_PORT", 8787, minimum=0, maximum=65535)
        tick_seconds = _read_float("NEXUS_SEED_TICK_SECONDS", 1.0, minimum=0.05)
        token = os.environ.get("NEXUS_SEED_WEBHOOK_TOKEN", "").strip() or None
        log_level = os.environ.get("NEXUS_SEED_LOG_LEVEL", "INFO").strip().upper()
        if not host:
            raise ApplicationConfigurationError(
                "NEXUS_SEED_WEBHOOK_HOST must not be empty"
            )
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ApplicationConfigurationError(
                "NEXUS_SEED_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
            )
        if host not in {"127.0.0.1", "localhost", "::1"} and token is None:
            raise ApplicationConfigurationError(
                "NEXUS_SEED_WEBHOOK_TOKEN is required when listening beyond localhost"
            )
        identity_id = os.environ.get("NEXUS_SEED_CONTROL_IDENTITY", "local-operator").strip()
        permissions = tuple(
            value.strip()
            for value in os.environ.get("NEXUS_SEED_CONTROL_PERMISSIONS", "command.*").split(",")
            if value.strip()
        )
        if not identity_id or not permissions:
            raise ApplicationConfigurationError(
                "control identity and at least one control permission are required"
            )
        phase6_enabled = _read_bool("NEXUS_SEED_PHASE6_ENABLED", True)
        cockpit_enabled = _read_bool("NEXUS_SEED_COCKPIT_ENABLED", True)
        return cls(
            data_dir=data_dir,
            host=host,
            port=port,
            webhook_token=token,
            tick_seconds=tick_seconds,
            log_level=log_level,
            control_identity_id=identity_id,
            control_permissions=permissions,
            phase6_enabled=phase6_enabled,
            cockpit_enabled=cockpit_enabled,
        )


def bootstrap_application(
    runtime: Runtime, settings: AppSettings, *, env_file: str | Path
) -> None:
    """Register the complete stack and default-on Phase 6 Process roles."""

    resource_root = settings.data_dir / "resources"
    action_root = settings.data_dir / "actions"
    resource_root.mkdir(parents=True, exist_ok=True)
    action_root.mkdir(parents=True, exist_ok=True)

    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)
    bootstrap_planning(runtime)
    bootstrap_resources(runtime, scope=ResourceScope.for_root(resource_root))
    bootstrap_observer(runtime)
    bootstrap_extension(runtime)
    bootstrap_autonomy(runtime)
    bootstrap_control(runtime)
    bootstrap_orchestration(runtime)
    bootstrap_persistent_being(runtime, enabled=settings.phase6_enabled)
    runtime.control_store.save_identity(
        HumanIdentity(
            identity_id=settings.control_identity_id,
            display_name="Configured control operator",
            permissions=settings.control_permissions,
            metadata={"source": "application configuration"},
        )
    )
    runtime.register_backend("local_file", LocalFileActionBackend(action_root))
    configure_llm(runtime, env_file=env_file)
    _configure_external_agents(runtime, env_file=env_file)


def _configure_external_agents(runtime: Runtime, *, env_file: str | Path) -> None:
    """Register configured A2A agents and the Skills routed to them.

    Skill problems are reported, never fatal: one malformed package must not
    stop the runtime from starting with the rest of its capabilities.
    """
    report = configure_external_agents(runtime, env_file=env_file)
    if report is None:
        return
    for failure in report.catalog.failures:
        logger.error("skill package rejected: %s", failure)
    for name, error in report.card_errors.items():
        logger.warning("A2A provider %s did not publish an agent card: %s", name, error)
    if report.unroutable:
        logger.warning(
            "no provider configured for skills: %s", ", ".join(report.unroutable)
        )
    logger.info(
        "external agent runtime: %d provider(s), %d skill(s) registered",
        len(report.providers),
        len(report.imported),
    )


def build_runtime(settings: AppSettings, *, env_file: str | Path = ".env") -> Runtime:
    """Create a durable Runtime in ``data_dir`` and bootstrap the full application."""

    _validate_data_dir(settings.data_dir)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    runtime = Runtime(
        settings.data_dir / "nexus_seed.db",
        construction_root=settings.data_dir / "construction",
        installation_root=settings.data_dir / "installed_extensions",
    )
    try:
        bootstrap_application(runtime, settings, env_file=env_file)
    except Exception:
        runtime.close()
        raise
    return runtime


def build_parser() -> argparse.ArgumentParser:
    """Build the application command-line parser."""

    parser = argparse.ArgumentParser(
        prog="nexus-seed",
        description="Run the durable NEXUS SEED webhook application.",
    )
    _add_connection_arguments(parser)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="recover and drain durable work once, then exit without serving HTTP",
    )
    mode.add_argument(
        "--check-llm",
        action="store_true",
        help="make one structured-output request to the configured LLM, then exit",
    )
    commands = parser.add_subparsers(dest="command", title="operational commands")

    serve = commands.add_parser("serve", help="run the webhook server (default)")
    _add_connection_arguments(serve, suppress_defaults=True)

    task = commands.add_parser("task", help="submit a natural-language task")
    _add_connection_arguments(task, suppress_defaults=True)
    task.add_argument("text", help="task text sent as a human_message Event")
    task.add_argument("--source-key", default=None, help="stable external id for deduplication")

    event = commands.add_parser("event", help="submit an arbitrary Event")
    _add_connection_arguments(event, suppress_defaults=True)
    event.add_argument("event_type", help="Event.type to submit")
    _add_payload_arguments(event)
    event.add_argument("--source-key", default=None, help="stable external id for deduplication")

    status = commands.add_parser("status", help="show durable work and process status")
    _add_connection_arguments(status, suppress_defaults=True)
    status.add_argument("--limit", type=_positive_int, default=10, help="recent rows to show")
    status.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    reviews = commands.add_parser("reviews", help="list pending human reviews")
    _add_connection_arguments(reviews, suppress_defaults=True)
    reviews.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    review = commands.add_parser("review", help="approve, reject, or modify a pending review")
    _add_connection_arguments(review, suppress_defaults=True)
    review.add_argument("review_id", help="id shown by 'nexus-seed reviews'")
    review.add_argument("decision", help="usually approve, reject, or modify")
    _add_payload_arguments(review, label="additional review payload")
    review.add_argument("--source-key", default=None, help="stable external id for deduplication")

    control = commands.add_parser("control", help="execute an explicit Phase 5G slash command")
    _add_connection_arguments(control, suppress_defaults=True)
    control.add_argument("text", help="for example: /status or /pause <work-id>")
    control.add_argument("--idempotency-key", default=None, help="stable command delivery id")
    return parser


def _add_connection_arguments(
    parser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    """Add dotenv/database/webhook overrides to a parser or subparser."""

    omitted = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--env-file",
        default=argparse.SUPPRESS if suppress_defaults else ".env",
        help="dotenv file (default: .env)",
    )
    parser.add_argument(
        "--data-dir",
        default=omitted,
        help="override NEXUS_SEED_DATA_DIR",
    )
    parser.add_argument(
        "--host",
        default=omitted,
        help="override webhook listen host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=omitted,
        help="override webhook port",
    )


def _add_payload_arguments(
    parser: argparse.ArgumentParser, *, label: str = "Event payload"
) -> None:
    """Add mutually exclusive inline/file JSON payload options."""

    payload = parser.add_mutually_exclusive_group()
    payload.add_argument(
        "--payload",
        dest="payload_json",
        default=None,
        metavar="JSON",
        help=f"{label} as a JSON object",
    )
    payload.add_argument(
        "--payload-file",
        default=None,
        metavar="PATH",
        help=f"read {label} JSON from a file",
    )


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


async def run(args: argparse.Namespace) -> int:
    """Run recovery and either exit once or serve webhook input continuously."""

    settings = _settings_from_args(args)
    command = getattr(args, "command", None)
    if command and (args.once or args.check_llm):
        raise ApplicationConfigurationError(
            "--once/--check-llm cannot be combined with an operational command"
        )

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    database_path = settings.data_dir / "nexus_seed.db"
    if command == "status":
        report = read_status(database_path, limit=args.limit)
        _print_operational_status(report, as_json=args.json)
        return 0
    if command == "reviews":
        reviews = read_pending_reviews(database_path)
        _print_reviews(reviews, as_json=args.json)
        return 0
    if command == "task":
        result = await asyncio.to_thread(
            submit_webhook_event,
            host=settings.host,
            port=settings.port,
            token=settings.webhook_token,
            event_type="human_message",
            payload={"text": args.text},
            source_event_key=args.source_key,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if command == "event":
        payload = _read_payload(args)
        result = await asyncio.to_thread(
            submit_webhook_event,
            host=settings.host,
            port=settings.port,
            token=settings.webhook_token,
            event_type=args.event_type,
            payload=payload,
            source_event_key=args.source_key,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if command == "review":
        review = resolve_pending_review(database_path, args.review_id)
        payload = build_review_payload(review, args.decision, _read_payload(args))
        result = await asyncio.to_thread(
            submit_webhook_event,
            host=settings.host,
            port=settings.port,
            token=settings.webhook_token,
            event_type=review.event_type,
            payload=payload,
            source_event_key=args.source_key,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if command == "control":
        result = await asyncio.to_thread(
            submit_control_command,
            host=settings.host,
            port=settings.port,
            token=settings.webhook_token,
            command=args.text,
            idempotency_key=args.idempotency_key,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "EXECUTED" else 1

    runtime = build_runtime(settings, env_file=args.env_file)
    server: WebhookServer | None = None
    try:
        await runtime.run_pending()
        await runtime.tick()
        if args.check_llm:
            return await _check_llm(runtime)
        if args.once:
            print(json.dumps(_status(runtime, settings), ensure_ascii=False, indent=2))
            return 0

        ingress = WebhookIngress(runtime.ingress, token=settings.webhook_token)
        cockpit = (
            CockpitService(
                runtime,
                phase6_enabled=settings.phase6_enabled,
                master_id=settings.control_identity_id,
                control_enabled=runtime.console is not None,
            )
            if settings.cockpit_enabled
            else None
        )
        server = await WebhookServer(
            ingress,
            host=settings.host,
            port=settings.port,
            console=runtime.console,
            control_identity_id=settings.control_identity_id,
            cockpit=cockpit,
        ).start()
        _print_started(runtime, settings, server.bound_port)

        while True:
            await asyncio.sleep(settings.tick_seconds)
            await runtime.tick()
    finally:
        if server is not None:
            await server.stop()
        runtime.close()


def _settings_from_args(args: argparse.Namespace) -> AppSettings:
    """Load dotenv settings and apply explicit CLI overrides."""

    settings = AppSettings.from_env(args.env_file)
    if args.data_dir is not None:
        settings = replace(
            settings,
            data_dir=Path(os.path.expandvars(args.data_dir)).expanduser().resolve(),
        )
    if args.host is not None:
        settings = replace(settings, host=args.host)
    if args.port is not None:
        if not 0 <= args.port <= 65535:
            raise ApplicationConfigurationError("--port must be between 0 and 65535")
        settings = replace(settings, port=args.port)
    if (
        settings.host not in {"127.0.0.1", "localhost", "::1"}
        and settings.webhook_token is None
    ):
        raise ApplicationConfigurationError(
            "NEXUS_SEED_WEBHOOK_TOKEN is required when listening beyond localhost"
        )

    return settings


def _read_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Read and validate an optional JSON-object command payload."""

    raw = getattr(args, "payload_json", None)
    payload_file = getattr(args, "payload_file", None)
    if payload_file is not None:
        raw = Path(payload_file).read_text(encoding="utf-8")
    if raw is None:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ApplicationConfigurationError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ApplicationConfigurationError("payload must be a JSON object")
    return payload


def _print_operational_status(report: dict[str, Any], *, as_json: bool) -> None:
    """Print a status snapshot for people or shell tooling."""

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print("NEXUS SEED status")
    print(f"  database: {report['database']}")
    if not report.get("initialized"):
        print("  state: not initialized")
        print(f"  hint: {report['hint']}")
        return
    counts = report["counts"]
    print(f"  events: {counts['events']}")
    print(f"  continuations: {counts['continuations']}")
    print(f"  pending reviews: {counts['pending_reviews']}")
    _print_count_line("work", report["work_by_status"])
    _print_count_line("processes", report["processes_by_status"])
    _print_count_line("deliveries", report["deliveries_by_status"])
    _print_count_line("provider calls", report["provider_invocations_by_status"])
    if report["recent_work"]:
        print("Recent work:")
        for work in report["recent_work"]:
            print(
                f"  {work['id']}  {work['status']}  {work['work_type']}"
                f"  priority={work['priority']}"
            )
    if report["recent_processes"]:
        print("Recent processes:")
        for process in report["recent_processes"]:
            error = f"  error={process['last_error']}" if process["last_error"] else ""
            print(
                f"  {process['id']}  {process['status']}  "
                f"{process['definition_name']}@{process['definition_version']}{error}"
            )


def _print_count_line(label: str, counts: dict[str, int]) -> None:
    rendered = ", ".join(f"{key}={value}" for key, value in counts.items()) or "none"
    print(f"  {label}: {rendered}")


def _print_reviews(reviews: list, *, as_json: bool) -> None:
    """Print durable review waiters without exposing unrelated state."""

    if as_json:
        print(
            json.dumps(
                {"count": len(reviews), "reviews": [item.to_dict() for item in reviews]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not reviews:
        print("No pending human reviews.")
        return
    print(f"Pending human reviews: {len(reviews)}")
    for review in reviews:
        print(
            f"  {review.review_id}  {review.event_type}  "
            f"process={review.process_definition}  since={review.created_at}"
        )
    print("Decide with: nexus-seed review <review-id> approve|reject")


async def _check_llm(runtime: Runtime) -> int:
    backend = runtime.backends.get("llm")
    if backend is None:
        print(
            json.dumps(
                {"success": False, "error": "LLM is disabled in .env"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    result = await backend.execute(
        BackendRequest(
            instruction=(
                "This is a connectivity check. Return an object whose status is "
                "the exact string 'ok'."
            ),
            output_schema={
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"const": "ok"}},
            },
        )
    )
    valid = result.success and result.parsed_output == {"status": "ok"}
    print(
        json.dumps(
            {
                "success": valid,
                "provider": getattr(backend, "provider", None),
                "model": result.model,
                "latency_ms": (
                    round(result.latency_ms, 2) if result.latency_ms is not None else None
                ),
                "response": result.parsed_output if result.success else None,
                "error": result.error if not result.success else (
                    None if valid else "LLM response did not match the connectivity schema"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if valid else 1


def _status(runtime: Runtime, settings: AppSettings) -> dict:
    return {
        "status": "idle",
        "database": str(settings.data_dir / "nexus_seed.db"),
        "llm_enabled": "llm" in runtime.backends,
        "delivery": runtime.get_delivery_health(),
        "work_requirements": len(runtime.get_work_requirements()),
        "capability_gaps": len(runtime.get_open_capability_gaps()),
    }


def _print_started(runtime: Runtime, settings: AppSettings, bound_port: int) -> None:
    token_state = "configured" if settings.webhook_token else "not configured"
    llm = runtime.backends.get("llm")
    llm_state = (
        f"{llm.provider}:{llm.model} at {llm.base_url or 'provider API'}"
        if llm is not None
        else "disabled (deterministic fallbacks)"
    )
    print("NEXUS SEED is running")
    print(f"  webhook: http://{settings.host}:{bound_port}/ingress/webhook")
    print(f"  control: http://{settings.host}:{bound_port}/control")
    if settings.cockpit_enabled:
        print(f"  cockpit: http://{settings.host}:{bound_port}/cockpit")
    print(f"  token:   {token_state}")
    print(f"  database: {settings.data_dir / 'nexus_seed.db'}")
    print(f"  LLM:     {llm_state}")
    print("Press Ctrl+C to stop. Durable work resumes on the next start.")


def _validate_data_dir(data_dir: Path) -> None:
    repository = Path.cwd().resolve()
    if (
        data_dir == repository
        or repository in data_dir.parents
        or data_dir in repository.parents
    ):
        raise ApplicationConfigurationError(
            "NEXUS_SEED_DATA_DIR must be outside the source repository"
        )


def _read_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApplicationConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ApplicationConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _read_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ApplicationConfigurationError(f"{name} must be a number") from exc
    if value < minimum:
        raise ApplicationConfigurationError(f"{name} must be at least {minimum}")
    return value


def _read_bool(name: str, default: bool) -> bool:
    """Read a strict boolean environment setting."""

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ApplicationConfigurationError(f"{name} must be true or false")


def main(argv: list[str] | None = None) -> int:
    """Console entry point for the runnable application."""

    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nNEXUS SEED stopped.")
        return 130
    except (
        ApplicationConfigurationError,
        LLMConfigurationError,
        OperationalCommandError,
        OSError,
    ) as exc:
        print(f"configuration/startup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
