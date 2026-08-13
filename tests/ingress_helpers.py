"""Shared scaffolding for the Phase 3D ingress tests (not a test module)."""

from __future__ import annotations

from nexus_seed.adapters.manual import ManualAdapter
from nexus_seed.backends import FakeLLMBackend, LocalFileActionBackend
from nexus_seed.ingress.service import IngressService
from nexus_seed.processes.actions import bootstrap_actions
from nexus_seed.processes.llm_interpret import bootstrap_llm_interpreter
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence

#: The natural-language message the closed-loop tests push in from outside.
ANALYSIS_MESSAGE = "D1のCD解析が完了しました。結果をレポートとして書き出してください。"

#: A message that drives the Phase 2C resistance work instead of an action.
TARGET_MESSAGE = "D1のCD targetを48nmから45nmへ変更しました。"


def analysis_proposal(confidence: float = 0.95, value: str = "within spec") -> dict:
    """LLM output recording ``D1_CD.analysis_result`` (requires outward work)."""
    return {
        "subject": "D1_CD",
        "predicate": "analysis_completed",
        "confidence": confidence,
        "rationale": "the message reports a completed CD analysis",
        "proposed_state_deltas": [
            {
                "entity": "D1_CD",
                "attribute": "analysis_result",
                "old_value": None,
                "new_value": value,
                "unit": None,
                "confidence": confidence,
            }
        ],
    }


def target_proposal(confidence: float = 0.95) -> dict:
    """LLM output recording a ``D1_CD.target`` change."""
    return {
        "subject": "D1_CD",
        "predicate": "target_changed",
        "confidence": confidence,
        "rationale": "the message reports an nm target change",
        "proposed_state_deltas": [
            {
                "entity": "D1_CD",
                "attribute": "target",
                "old_value": 48,
                "new_value": 45,
                "unit": "nm",
                "confidence": confidence,
            }
        ],
    }


def full_stack(runtime, action_root=None, *, llm_script=None, policy=None):
    """Register perception, work intelligence and the action boundary."""
    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)
    bootstrap_actions(runtime, policy=policy)
    bootstrap_llm_interpreter(runtime, FakeLLMBackend(script=llm_script))
    backend = None
    if action_root is not None:
        backend = LocalFileActionBackend(action_root)
        runtime.register_backend("local_file", backend)
    return backend


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


def instances_named(runtime, name) -> list:
    """Return every instance of the definition ``name``, oldest first."""
    return [i for i in runtime.process_store.all_instances() if i.definition_name == name]
