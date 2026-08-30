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

from .adapters.file_watch import LocalFileAdapter
from .adapters.webhook import WebhookIngress, WebhookServer
from .backends.base import BackendRequest
from .cockpit import CockpitService
from .config_report import build_report, format_report
from .llm_config import LLMConfigurationError, configure_llm, load_env_file
from .knowledge import CONTEXT_DIR, KnowledgeLoop, ReasoningProjectAgent
from .operations import (
    OperationalCommandError,
    read_status,
    submit_webhook_event,
)
from .orchestrator import (
    Agent,
    AssignmentStatus,
    Project,
    ProjectOrchestrator,
    RoutingDecision,
    InProcessAgentRuntime,
)
from .orchestrator_config import ProjectAgentConfigurationError, build_orchestrator
from .processes.project_orchestration import bootstrap_project_orchestration
from .processes.resources import bootstrap_observer, bootstrap_resources
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
    #: Who is at the keyboard.  Recorded as the actor on review decisions and
    #: self-question answers; no longer authorized against anything, because
    #: there is no longer a command surface to authorize.
    operator_id: str = "local-operator"
    #: Human Interface Layer; False removes every Cockpit HTTP route.
    cockpit_enabled: bool = True
    #: Close the Knowledge -> Project -> Agent -> Knowledge loop.  With no LLM
    #: it still records evidence but proposes nothing.
    knowledge_loop_enabled: bool = True
    knowledge_poll_seconds: float = 60.0

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
        operator_id = (
            os.environ.get("NEXUS_SEED_OPERATOR_ID")
            or os.environ.get("NEXUS_SEED_CONTROL_IDENTITY")
            or "local-operator"
        ).strip()
        if not operator_id:
            raise ApplicationConfigurationError("an operator id is required")
        cockpit_enabled = _read_bool("NEXUS_SEED_COCKPIT_ENABLED", True)
        knowledge_loop_enabled = _read_bool("NEXUS_SEED_KNOWLEDGE_LOOP_ENABLED", True)
        knowledge_poll_seconds = _read_float(
            "NEXUS_SEED_KNOWLEDGE_POLL_SECONDS", 60.0, minimum=0.05
        )
        return cls(
            data_dir=data_dir,
            host=host,
            port=port,
            webhook_token=token,
            tick_seconds=tick_seconds,
            log_level=log_level,
            operator_id=operator_id,
            cockpit_enabled=cockpit_enabled,
            knowledge_loop_enabled=knowledge_loop_enabled,
            knowledge_poll_seconds=knowledge_poll_seconds,
        )


def bootstrap_application(
    runtime: Runtime, settings: AppSettings, *, env_file: str | Path
) -> None:
    """Register the Project-centered application stack."""

    resource_root = settings.data_dir / "resources"
    resource_root.mkdir(parents=True, exist_ok=True)

    # Resource indexing/extraction feeds the Knowledge Runtime.  The legacy
    # World State interpreter is intentionally not part of this application.
    bootstrap_resources(runtime, scope=ResourceScope.for_root(resource_root))
    bootstrap_observer(runtime)
    configure_llm(runtime, env_file=env_file)
    from .observation_sources import ObservationSourceService

    runtime.observation_sources = ObservationSourceService(
        runtime, data_root=settings.data_dir
    )
    orchestrator = _configure_project_orchestrator(runtime, settings, env_file=env_file)
    _configure_knowledge_loop(runtime, settings, orchestrator, resource_root=resource_root)


def _configure_project_orchestrator(
    runtime: Runtime, settings: AppSettings, *, env_file: str | Path
) -> ProjectOrchestrator:
    """Give the runtime its single Project Orchestrator.

    It shares the runtime's database, so Projects, Agents and the audited A2A
    channel live beside everything else and are recovered the same way.
    """
    orchestrator = build_orchestrator(runtime.db, env_file=env_file)
    if isinstance(orchestrator.agent_runtime, InProcessAgentRuntime):
        # The local Agent is intentionally bounded to reasoning over the
        # Project context.  A future A2A runtime replaces this interface.
        orchestrator.agent_runtime.behaviour = ReasoningProjectAgent(
            runtime.backends.get("llm")
        )
    bootstrap_project_orchestration(runtime, orchestrator)
    return orchestrator


def _configure_knowledge_loop(
    runtime: Runtime,
    settings: AppSettings,
    orchestrator: ProjectOrchestrator,
    *,
    resource_root: Path,
) -> KnowledgeLoop | None:
    """Wire the autonomous Knowledge loop and its authorized local folder."""
    if not settings.knowledge_loop_enabled:
        return None
    adapter = LocalFileAdapter(resource_root, adapter_id="knowledge_local_file")
    runtime.register_adapter(adapter)
    context_root = settings.data_dir / CONTEXT_DIR
    loop = KnowledgeLoop(
        runtime,
        orchestrator,
        backend=runtime.backends.get("llm"),
        context_root=context_root,
    )
    # Created empty rather than waited for: a person needs somewhere to write
    # what the words mean and what a good state looks like, and an existing
    # file with a prompt in it is a better invitation than a missing one.
    loop.context_documents.ensure()
    runtime.knowledge_loop = loop
    logger.info(
        "knowledge loop enabled; observing %s, reading context from %s",
        resource_root,
        context_root,
    )
    return loop


def build_runtime(settings: AppSettings, *, env_file: str | Path = ".env") -> Runtime:
    """Create a durable Runtime and bootstrap the Project application."""

    _validate_data_dir(settings.data_dir)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    runtime = Runtime(settings.data_dir / "nexus_seed.db")
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

    task = commands.add_parser(
        "task", help="submit a natural-language request to the Project Orchestrator"
    )
    _add_connection_arguments(task, suppress_defaults=True)
    task.add_argument("text", help="task text sent as a human_message Event")
    task.add_argument("--source-key", default=None, help="stable external id for deduplication")

    project = commands.add_parser(
        "project", help="route a request through the Project Orchestrator"
    )
    _add_connection_arguments(project, suppress_defaults=True)
    project.add_argument("text", help="what you want done, in your own words")
    project.add_argument(
        "--priority", type=int, default=0, help="how urgent this is (higher runs first)"
    )
    project.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="report as soon as the agent has taken the work, without waiting it out",
    )
    project.add_argument(
        "--wait-seconds",
        type=float,
        default=1800.0,
        help="how long to watch the project before reporting (default: 1800)",
    )
    project.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    event = commands.add_parser("event", help="submit an arbitrary Event")
    _add_connection_arguments(event, suppress_defaults=True)
    event.add_argument("event_type", help="Event.type to submit")
    _add_payload_arguments(event)
    event.add_argument("--source-key", default=None, help="stable external id for deduplication")

    config = commands.add_parser(
        "config", help="show the resolved configuration, grouped by what it is for"
    )
    config.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    status = commands.add_parser("status", help="show durable work and process status")
    _add_connection_arguments(status, suppress_defaults=True)
    status.add_argument("--limit", type=_positive_int, default=10, help="recent rows to show")
    status.add_argument("--json", action="store_true", help="emit machine-readable JSON")

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
    if command == "config":
        report = build_report(args.env_file)
        print(
            json.dumps(report, ensure_ascii=False, indent=2)
            if args.json
            else format_report(report),
            end="" if not args.json else "\n",
        )
        return 0
    if command == "status":
        report = read_status(database_path, limit=args.limit)
        _print_operational_status(report, as_json=args.json)
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
    if command == "project":
        return await _run_project(args, settings)
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
    runtime = build_runtime(settings, env_file=args.env_file)
    orchestrator = getattr(runtime, "project_orchestrator", None)
    knowledge_loop = getattr(runtime, "knowledge_loop", None)
    server: WebhookServer | None = None
    try:
        await runtime.run_pending()
        await runtime.tick()
        if orchestrator is not None:
            # Recovery for Projects is the same loop that runs them: whatever an
            # Agent did while NEXUS SEED was down is picked up here.
            await orchestrator.reconcile()
        if knowledge_loop is not None:
            if runtime.observation_sources is not None:
                await runtime.observation_sources.poll_due()
            await knowledge_loop.start_file_observer(
                poll_interval=settings.knowledge_poll_seconds
            )
            await knowledge_loop.reconcile()
        if args.check_llm:
            return await _check_llm(runtime)
        if args.once:
            print(json.dumps(_status(runtime, settings), ensure_ascii=False, indent=2))
            return 0

        ingress = WebhookIngress(runtime.ingress, token=settings.webhook_token)
        cockpit = (
            CockpitService(
                runtime,
                master_id=settings.operator_id,
            )
            if settings.cockpit_enabled
            else None
        )
        server = await WebhookServer(
            ingress,
            host=settings.host,
            port=settings.port,
            cockpit=cockpit,
        ).start()
        _print_started(runtime, settings, server.bound_port)

        while True:
            await asyncio.sleep(settings.tick_seconds)
            await runtime.tick()
            if orchestrator is not None:
                await orchestrator.reconcile()
            if knowledge_loop is not None:
                if runtime.observation_sources is not None:
                    await runtime.observation_sources.poll_due()
                await knowledge_loop.reconcile()
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


async def _run_project(args: argparse.Namespace, settings: AppSettings) -> int:
    """Route one request through the Project Orchestrator and report where it got.

    The explicit door into the orchestrator, so by default it watches the
    Project through rather than reporting "accepted" — pass ``--no-wait`` for
    the same hand-off a resident NEXUS SEED does.  Either way the Project, its
    Agent and the audited A2A channel are written to SQLite as they change, so
    interrupting the wait loses the report, never the project.
    """
    _validate_data_dir(settings.data_dir)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    orchestrator = build_orchestrator(
        settings.data_dir / "nexus_seed.db", env_file=args.env_file
    )
    try:
        decision, project = await orchestrator.submit(args.text, priority=args.priority)
        if project is not None and args.wait:
            project = await orchestrator.settle(project.id, timeout=args.wait_seconds)
        agent = (
            orchestrator.agents.for_project(project.id) if project is not None else None
        )
    finally:
        orchestrator.close()
    _print_project(decision, project, agent, as_json=args.json)
    return 0


def _print_project(
    decision: RoutingDecision,
    project: Project | None,
    agent: Agent | None,
    *,
    as_json: bool,
) -> None:
    """Show what the orchestrator decided and where the project stands now."""
    if as_json:
        print(
            json.dumps(
                {
                    "routing": decision.to_dict(),
                    "project": project.to_dict() if project else None,
                    "agent": agent.to_dict() if agent else None,
                    "assignment": (
                        agent.assignment.to_dict()
                        if agent is not None and agent.assignment is not None
                        else None
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print(f"Routing: {decision.action.value}")
    if project is None:
        print(f"Reason:  {decision.reason or 'no project work was needed'}")
        return
    print(f"Project: {project.id}")
    print(f"Agent:   {agent.agent_id if agent else '(none assigned)'}")
    print(f"Status:  {project.status.value}")
    print(f"Goal:    {project.goal}")
    if project.summary:
        print(f"Summary: {project.summary}")
    for blocker in project.current_blockers:
        print(f"Blocked: {blocker['kind']} - {blocker['reason']}")
    assignment = agent.assignment if agent else None
    if assignment is not None and assignment.status is AssignmentStatus.DISPATCHED:
        print("Working:  the agent has the project and has not answered yet")
    unavailable = (agent.metadata.get("unavailable") if agent else None) or {}
    if unavailable and (assignment is None or assignment.is_open):
        # The Project is untouched and still delegable; the runtime was not there.
        print(f"Agent unavailable: {unavailable.get('reason', '')}")
        print("The project keeps its state and the hand-over is retried.")


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
    print(f"  projects: {counts['projects']}")
    print(f"  agents: {counts['agents']}")
    print(f"  knowledge revisions: {counts['knowledge_revisions']}")
    _print_count_line("projects", report["projects_by_status"])
    _print_count_line("agents", report["agents_by_status"])
    _print_count_line("deliveries", report["deliveries_by_status"])
    _print_count_line("knowledge", report["knowledge_by_kind"])
    if report["recent_projects"]:
        print("Recent projects:")
        for project in report["recent_projects"]:
            print(
                f"  {project['id']}  {project['status']}  {project['goal']}"
                f"  priority={project['priority']}"
            )
    if report["recent_agents"]:
        print("Recent agents:")
        for agent in report["recent_agents"]:
            print(
                f"  {agent['agent_id']}  {agent['status']}  "
                f"project={agent['project_id']} runtime={agent['runtime']}"
            )


def _print_count_line(label: str, counts: dict[str, int]) -> None:
    rendered = ", ".join(f"{key}={value}" for key, value in counts.items()) or "none"
    print(f"  {label}: {rendered}")


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
    orchestrator = runtime.project_orchestrator
    knowledge_loop = runtime.knowledge_loop
    projects = orchestrator.projects.all() if orchestrator is not None else []
    agents = orchestrator.agent_store.all() if orchestrator is not None else []
    knowledge = knowledge_loop.ledger.all_heads() if knowledge_loop is not None else []
    return {
        "status": "idle",
        "database": str(settings.data_dir / "nexus_seed.db"),
        "llm_enabled": "llm" in runtime.backends,
        "delivery": runtime.get_delivery_health(),
        "projects": len(projects),
        "agents": len(agents),
        "knowledge_items": len(knowledge),
        "pending_reviews": sum(item.status == "PENDING_REVIEW" for item in knowledge),
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
    orchestrator = runtime.project_orchestrator
    print(
        "  projects: nexus-seed task goes to the Project Orchestrator "
        f"({orchestrator.agent_runtime.name} agents)"
    )
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


def _tolerate_unprintable_characters() -> None:
    """Never let a console codepage turn an Agent's own words into a crash.

    Project summaries come from a Project Agent, so they can contain anything a
    model wrote.  The encoding itself is left alone — only the failure mode
    changes, from raising to substituting.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):  # pragma: no cover - unusual stream
            logger.debug("could not relax encoding errors on %r", stream)


def main(argv: list[str] | None = None) -> int:
    """Console entry point for the runnable application."""

    _tolerate_unprintable_characters()
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
        ProjectAgentConfigurationError,
        OSError,
    ) as exc:
        print(f"configuration/startup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
