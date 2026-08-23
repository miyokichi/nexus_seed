"""Shared scaffolding for the Phase 3D ingress tests (not a test module)."""

from __future__ import annotations

from nexus_seed.adapters.manual import ManualAdapter
from nexus_seed.ingress.service import IngressService

#: The natural-language message the closed-loop tests push in from outside.
ANALYSIS_MESSAGE = "D1のCD解析が完了しました。結果をレポートとして書き出してください。"

#: A message that drives the Phase 2C resistance work instead of an action.
TARGET_MESSAGE = "D1のCD targetを48nmから45nmへ変更しました。"


def ingress(runtime) -> IngressService:
    """Build the ingress service for ``runtime``."""
    return IngressService(runtime)


def manual_envelope(
    *,
    event_type: str = "human_message",
    source_event_key: str = "demo-001",
    payload: dict | None = None,
    adapter_id: str = "manual",
):
    """Build a manual envelope with sensible test defaults."""
    return ManualAdapter(adapter_id).envelope(
        event_type=event_type,
        source_event_key=source_event_key,
        payload=payload if payload is not None else {"text": ANALYSIS_MESSAGE},
    )
