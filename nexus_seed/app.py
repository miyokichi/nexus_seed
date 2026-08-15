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

from .adapters.webhook import WebhookIngress, WebhookServer
from .backends.action import LocalFileActionBackend
from .backends.base import BackendRequest
from .llm_config import LLMConfigurationError, configure_llm, load_env_file
from .processes.autonomy import bootstrap_autonomy
from .processes.extension import bootstrap_extension
from .processes.planning import bootstrap_planning
from .processes.resources import bootstrap_observer, bootstrap_resources
from .processes.semantic import bootstrap_semantic
from .processes.work_intelligence import bootstrap_work_intelligence
from .resources.scope import ResourceScope
from .runtime.runtime import Runtime


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
        return cls(data_dir, host, port, token, tick_seconds, log_level)


def bootstrap_application(
    runtime: Runtime, settings: AppSettings, *, env_file: str | Path
) -> None:
    """Register the complete Phase 1–5D process stack and bounded backends."""

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
    runtime.register_backend("local_file", LocalFileActionBackend(action_root))
    configure_llm(runtime, env_file=env_file)


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
    parser.add_argument("--env-file", default=".env", help="dotenv file (default: .env)")
    parser.add_argument("--data-dir", default=None, help="override NEXUS_SEED_DATA_DIR")
    parser.add_argument("--host", default=None, help="override webhook listen host")
    parser.add_argument("--port", type=int, default=None, help="override webhook port")
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
    return parser


async def run(args: argparse.Namespace) -> int:
    """Run recovery and either exit once or serve webhook input continuously."""

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

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
        server = await WebhookServer(
            ingress,
            host=settings.host,
            port=settings.port,
        ).start()
        _print_started(runtime, settings, server.bound_port)

        while True:
            await asyncio.sleep(settings.tick_seconds)
            await runtime.tick()
    finally:
        if server is not None:
            await server.stop()
        runtime.close()


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


def main(argv: list[str] | None = None) -> int:
    """Console entry point for the runnable application."""

    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nNEXUS SEED stopped.")
        return 130
    except (ApplicationConfigurationError, LLMConfigurationError, OSError) as exc:
        print(f"configuration/startup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
