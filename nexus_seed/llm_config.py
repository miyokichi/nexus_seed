"""Environment-file configuration for the optional LLM integration.

This module is application wiring, not Runtime mechanism.  It deliberately
keeps provider configuration and domain-specific LLM adapters outside
``Runtime`` while giving applications one place to connect all of them.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .backends.llm import LLMBackend

if TYPE_CHECKING:
    from .runtime.runtime import Runtime


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class LLMConfigurationError(ValueError):
    """Raised when LLM settings are enabled but incomplete or invalid."""


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """Validated LLM connection settings loaded from the environment."""

    enabled: bool = False
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-5"
    max_tokens: int = 8192
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: str | None = None
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> LLMSettings:
        """Load ``env_file`` and return validated settings.

        Values already present in the process environment take precedence over
        the file, matching common ``.env`` behaviour and allowing deployment
        secrets to override local development settings.
        """

        load_env_file(env_file)
        enabled = _read_bool("NEXUS_SEED_LLM_ENABLED", default=False)
        provider = os.environ.get("NEXUS_SEED_LLM_PROVIDER", "anthropic").strip()
        model = os.environ.get("NEXUS_SEED_LLM_MODEL", "claude-sonnet-4-5").strip()
        api_key_env = os.environ.get(
            "NEXUS_SEED_LLM_API_KEY_ENV", "ANTHROPIC_API_KEY"
        ).strip()
        base_url = os.environ.get("NEXUS_SEED_LLM_BASE_URL", "").strip() or None
        max_tokens = _read_positive_int("NEXUS_SEED_LLM_MAX_TOKENS", default=8192)
        timeout_seconds = _read_positive_float(
            "NEXUS_SEED_LLM_TIMEOUT_SECONDS", default=120.0
        )
        provider = provider.lower().replace("-", "_")
        if provider == "openai":
            provider = "openai_compatible"

        if not provider:
            raise LLMConfigurationError("NEXUS_SEED_LLM_PROVIDER must not be empty")
        if not model:
            raise LLMConfigurationError("NEXUS_SEED_LLM_MODEL must not be empty")
        if not _ENV_NAME.fullmatch(api_key_env):
            raise LLMConfigurationError(
                "NEXUS_SEED_LLM_API_KEY_ENV must name a valid environment variable"
            )
        if provider not in {"anthropic", "openai_compatible"}:
            raise LLMConfigurationError(
                "NEXUS_SEED_LLM_PROVIDER must be 'anthropic' or 'openai_compatible'"
            )
        if provider == "openai_compatible":
            parsed_url = urlparse(base_url or "")
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise LLMConfigurationError(
                    "NEXUS_SEED_LLM_BASE_URL must be an http(s) URL for "
                    "openai_compatible"
                )
        if (
            enabled
            and provider == "anthropic"
            and not os.environ.get(api_key_env, "").strip()
        ):
            raise LLMConfigurationError(
                f"NEXUS_SEED_LLM_ENABLED is true but {api_key_env} is empty"
            )

        return cls(
            enabled=enabled,
            provider=provider,
            model=model,
            max_tokens=max_tokens,
            api_key_env=api_key_env,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )


def load_env_file(path: str | Path = ".env") -> Path | None:
    """Load a small, dependency-free subset of dotenv syntax.

    Blank lines and comments are ignored; ``export NAME=value`` and quoted
    values are accepted.  Existing process environment values are never
    overwritten.  A missing file is allowed so installed deployments can use
    environment variables only.
    """

    env_path = Path(path)
    if not env_path.is_file():
        return None

    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise LLMConfigurationError(
                f"{env_path}:{line_number}: expected NAME=value"
            )
        name, value = line.split("=", 1)
        name = name.strip()
        if not _ENV_NAME.fullmatch(name):
            raise LLMConfigurationError(
                f"{env_path}:{line_number}: invalid environment variable name {name!r}"
            )
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)
    return env_path


def configure_llm(
    runtime: Runtime,
    *,
    env_file: str | Path = ".env",
) -> LLMBackend | None:
    """Configure NEXUS SEED's single reasoning backend.

    When disabled, no LLM process or adapter is registered and deterministic
    fallbacks remain active.  Call this again whenever a Runtime is rebuilt;
    backend registrations are intentionally in-memory rather than persisted.
    """

    settings = LLMSettings.from_env(env_file)
    if not settings.enabled:
        return None

    backend = LLMBackend(
        provider=settings.provider,
        model=settings.model,
        api_key_env=settings.api_key_env,
        max_tokens=settings.max_tokens,
        base_url=settings.base_url,
        timeout_seconds=settings.timeout_seconds,
    )
    runtime.register_backend("llm", backend)
    return backend


def _read_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise LLMConfigurationError(
        f"{name} must be one of: true/false, yes/no, on/off, 1/0"
    )


def _read_positive_int(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise LLMConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise LLMConfigurationError(f"{name} must be greater than zero")
    return value


def _read_positive_float(name: str, *, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise LLMConfigurationError(f"{name} must be a number") from exc
    if value <= 0:
        raise LLMConfigurationError(f"{name} must be greater than zero")
    return value


__all__ = [
    "LLMConfigurationError",
    "LLMSettings",
    "configure_llm",
    "load_env_file",
]
