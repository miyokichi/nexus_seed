"""Command-line entry point for the Knowledge Runtime.

    python -m nexus_seed.knowledge_cli <subcommand> --db knowledge.db ...
    # or, once installed: nexus-seed-knowledge <subcommand> --db knowledge.db ...

Every subcommand is a thin wrapper over the plain Python API in
``nexus_seed.knowledge`` (see the README's "Knowledge Runtime quick start"
for the same walkthrough as importable code) — this file adds no behaviour
of its own beyond argument parsing, opening the database, and printing the
result. One ``--db`` SQLite file is shared by the Knowledge Ledger *and* the
Project Orchestrator (`nexus_seed.storage.Database` creates every table from
one schema), so ``submit`` can hand a detected Signal straight to a real
``ProjectOrchestrator`` on the same file.

LLM-backed subcommands (``consolidate``, ``extract-principle``,
``counterexample``, ``predict``, ``signals``, ``submit``) read the same
``NEXUS_SEED_LLM_*`` settings the rest of NEXUS SEED uses (see
``nexus_seed/llm_config.py`` / ``.env.example``); ``--llm``/``--no-llm``
override that for one call. Every one of them also runs with no backend at
all — they just fall back to the conservative, non-fabricating behaviour
documented on each class (see ``nexus_seed/knowledge/consolidation.py`` etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .backends.base import ExecutionBackend
from .backends.llm import LLMBackend
from .knowledge.consolidation import Consolidator, select_candidates
from .knowledge.context_assessment import CANDIDATE_PENDING_REVIEW, FINDING_KINDS
from .knowledge.experience import advisories_for, record_agent_experience
from .knowledge.goal_bridge import GapRiskOpportunityDetector, GoalBridge, Signal
from .knowledge.ingest_pptx import ingest_pptx_file
from .knowledge.ledger import KnowledgeLedger
from .knowledge.models import (
    KIND_PRINCIPLE,
    STATUS_SUPPORTED,
    STATUS_VALIDATED,
    KnowledgeRevision,
    Relation,
)
from .knowledge.principles import (
    CounterexampleSearcher,
    PredictionEngine,
    PrincipleExtractor,
    apply_prediction_feedback,
    evaluate_prediction,
    record_support,
    refine_principle,
)
from .knowledge.projection import WorldStateProjection, annotate_world_fact, diff_world_views
from .knowledge.bootstrap_context import CONTEXT_DIR
from .llm_config import LLMSettings
from .storage import Database, EventStore, KnowledgeStore


# --- shared helpers ----------------------------------------------------------


def _open_ledger(db_path: str) -> KnowledgeLedger:
    return KnowledgeLedger(KnowledgeStore(Database(db_path)))


def _parse_dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def _parse_value(raw: str) -> Any:
    """A CLI value: JSON if it parses (numbers, bools, objects), else the raw string."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _read_content(args: argparse.Namespace) -> str:
    if args.content is not None:
        return args.content
    if args.content_file:
        return Path(args.content_file).read_text(encoding="utf-8")
    return sys.stdin.read()


def _build_backend(args: argparse.Namespace) -> ExecutionBackend | None:
    """The real LLM backend, honouring ``NEXUS_SEED_LLM_*`` plus ``--llm``/``--no-llm``."""
    if getattr(args, "no_llm", False):
        return None
    settings = LLMSettings.from_env(getattr(args, "env_file", ".env") or ".env")
    if not settings.enabled and not getattr(args, "llm", False):
        return None
    return LLMBackend(
        provider=settings.provider,
        model=settings.model,
        api_key_env=settings.api_key_env,
        max_tokens=settings.max_tokens,
        base_url=settings.base_url,
        timeout_seconds=settings.timeout_seconds,
    )


def _preview(value: Any, width: int = 100) -> str:
    """One line of content, safe to print under an indent."""
    text = value if isinstance(value, str) else str(value)
    return " ".join(text.split())[:width]


def _fmt_line(rev: KnowledgeRevision) -> str:
    status = rev.status or "-"
    return (
        f"{rev.knowledge_id}  v{rev.revision}  [{rev.kind}/{status}]  "
        f"{_preview(rev.content.value, 70)}"
    )


def _print_rev(rev: KnowledgeRevision, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(rev.to_dict(), ensure_ascii=False, indent=2))
        return
    print(f"id:            {rev.id}")
    print(f"knowledge_id:  {rev.knowledge_id}")
    print(f"revision:      {rev.revision}")
    print(f"kind:          {rev.kind}")
    print(f"status:        {rev.status or '-'}")
    print(f"source:        {rev.source.type} / {rev.source.ref or '-'}")
    print(f"recorded_at:   {rev.recorded_at.isoformat()}")
    print(f"valid:         {rev.valid_from.isoformat() if rev.valid_from else '(open)'} -> "
          f"{rev.valid_to.isoformat() if rev.valid_to else '(open)'}")
    print(f"parents:       {rev.parents or '-'}")
    print(f"derived_from:  {rev.derived_from or '-'}")
    for relation in rev.relations:
        print(f"relation:      {relation.type} -> {relation.target}")
    for annotation in rev.annotations:
        print(f"annotation:    {annotation.kind} = {json.dumps(annotation.value, ensure_ascii=False)}")
    if rev.metadata:
        print(f"metadata:      {json.dumps(rev.metadata, ensure_ascii=False)}")
    print("content:")
    print(f"  {rev.content.value}")


def _select_cases(ledger: KnowledgeLedger, args: argparse.Namespace) -> list[KnowledgeRevision]:
    if args.ids:
        cases = [ledger.head(i) for i in args.ids]
        missing = [i for i, c in zip(args.ids, cases) if c is None]
        if missing:
            raise ValueError(f"unknown knowledge_id(s): {', '.join(missing)}")
        return cases
    return select_candidates(
        ledger,
        about=args.about,
        kinds=tuple(args.kind) if args.kind else ("raw", "consolidated_memory", "experience"),
        max_items=args.max_items,
    )


def _select_principles(ledger: KnowledgeLedger, ids: list[str] | None) -> list[KnowledgeRevision]:
    if ids:
        principles = [ledger.head(i) for i in ids]
        missing = [i for i, p in zip(ids, principles) if p is None]
        if missing:
            raise ValueError(f"unknown knowledge_id(s): {', '.join(missing)}")
        return principles
    return [p for p in ledger.by_kind(KIND_PRINCIPLE) if p.status in (STATUS_SUPPORTED, STATUS_VALIDATED)]


# --- subcommands: K1 ledger ---------------------------------------------------


def cmd_record(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    rev = ledger.record(
        _read_content(args),
        source_type=args.source_type,
        source_ref=args.source_ref,
        kind=args.kind,
        knowledge_id=args.knowledge_id,
        valid_from=_parse_dt(args.valid_from),
        valid_to=_parse_dt(args.valid_to),
    )
    if args.about:
        rev = ledger.relate(rev.knowledge_id, Relation(type="about", target=args.about))
    _print_rev(rev, as_json=args.json)
    return 0


def cmd_revise(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    # Only pass fields the caller actually gave: KnowledgeLedger.revise() carries
    # every omitted field forward from HEAD unchanged (a sentinel default tells
    # "not given" from "given as empty"), so an unset kwarg here must stay unset.
    kwargs: dict[str, Any] = {"reason": args.reason}
    if args.content is not None or args.content_file:
        kwargs["value"] = _read_content(args)
    if args.valid_from:
        kwargs["valid_from"] = _parse_dt(args.valid_from)
    if args.valid_to:
        kwargs["valid_to"] = _parse_dt(args.valid_to)
    if args.status is not None:
        kwargs["status"] = args.status
    rev = ledger.revise(args.knowledge_id, **kwargs)
    _print_rev(rev, as_json=args.json)
    return 0


def cmd_relate(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    rev = ledger.relate(args.knowledge_id, Relation(type=args.type, target=args.target))
    _print_rev(rev, as_json=args.json)
    return 0


def cmd_conflict(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    ledger.mark_conflict(args.knowledge_id_a, args.knowledge_id_b, reason=args.reason)
    print(f"marked CONFLICT: {args.knowledge_id_a} <-> {args.knowledge_id_b}")
    return 0


def cmd_fact(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    rev = annotate_world_fact(
        ledger,
        args.knowledge_id,
        entity=args.entity,
        attribute=args.attribute,
        value=_parse_value(args.value),
        confidence=args.confidence,
    )
    _print_rev(rev, as_json=args.json)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    if args.history:
        history = ledger.history(args.knowledge_id)
        if not history:
            print(f"no such knowledge_id: {args.knowledge_id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps([r.to_dict() for r in history], ensure_ascii=False, indent=2))
        else:
            for rev in history:
                print(_fmt_line(rev))
        return 0
    rev = ledger.head(args.knowledge_id)
    if rev is None:
        print(f"no such knowledge_id: {args.knowledge_id}", file=sys.stderr)
        return 1
    _print_rev(rev, as_json=args.json)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    revs = ledger.store.all_revisions() if args.all_revisions else ledger.all_heads()
    if args.kind:
        revs = [r for r in revs if r.kind == args.kind]
    if args.status:
        revs = [r for r in revs if r.status == args.status]
    if args.source_type:
        revs = [r for r in revs if r.source.type == args.source_type]
    if args.about:
        revs = [r for r in revs if any(rel.type == "about" and rel.target == args.about for rel in r.relations)]
    if args.json:
        print(json.dumps([r.to_dict() for r in revs], ensure_ascii=False, indent=2))
    else:
        for rev in revs:
            print(_fmt_line(rev))
        print(f"({len(revs)} shown)")
    return 0


# --- subcommands: K2 world projection -----------------------------------------


def cmd_view(args: argparse.Namespace) -> int:
    if args.as_of and args.valid_at:
        raise ValueError("pass at most one of --as-of / --valid-at")
    ledger = _open_ledger(args.db)
    view = WorldStateProjection(ledger).view(as_of=_parse_dt(args.as_of), valid_at=_parse_dt(args.valid_at))
    if args.json:
        print(json.dumps({
            "facts": {f"{e}.{a}": fact.value for (e, a), fact in view.facts.items()},
            "conflicts": {
                f"{e}.{a}": [f.value for f in facts] for (e, a), facts in view.conflicts.items()
            },
        }, ensure_ascii=False, indent=2))
        return 0
    for (entity, attribute), fact in sorted(view.facts.items()):
        flag = " [CONFLICT]" if view.is_conflicted(entity, attribute) else ""
        print(f"{entity}.{attribute} = {fact.value!r}  ({fact.knowledge_id}){flag}")
    for (entity, attribute), facts in sorted(view.conflicts.items()):
        values = ", ".join(f"{f.value!r} <- {f.knowledge_id}" for f in facts)
        print(f"  conflict on {entity}.{attribute}: {values}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    if bool(args.before) == bool(args.before_valid_at):
        raise ValueError("pass exactly one of --before / --before-valid-at")
    ledger = _open_ledger(args.db)
    projection = WorldStateProjection(ledger)
    before = projection.view(as_of=_parse_dt(args.before), valid_at=_parse_dt(args.before_valid_at))
    after = projection.view(as_of=_parse_dt(args.after), valid_at=_parse_dt(args.after_valid_at))
    diff = diff_world_views(before, after)
    for change in diff.changes:
        print(f"{change.change:8} {change.entity}.{change.attribute}: "
              f"{change.old_value!r} -> {change.new_value!r}")
    if not diff.changes:
        print("(no change)")
    if args.emit_events:
        events = diff.to_events(source=args.event_source)
        store = EventStore(ledger.store.db)
        for event in events:
            store.append(event)
        print(f"emitted {len(events)} state_changed event(s) into the events table")
    return 0


# --- subcommands: local file ingestion ----------------------------------------


def cmd_pptx(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    paths = [Path(f) for f in args.files]
    if args.dir:
        paths.extend(sorted(Path(args.dir).rglob("*.pptx")))
    if not paths:
        print("no .pptx files given (pass file paths and/or --dir)", file=sys.stderr)
        return 2
    total = 0
    for path in paths:
        if not path.exists():
            print(f"skip (not found): {path}", file=sys.stderr)
            continue
        created = ingest_pptx_file(ledger, path, about=args.about)
        ids = ", ".join(r.knowledge_id for r in created) or "-"
        print(f"{path}: {len(created)} slide(s) -> Knowledge ({ids})")
        total += len(created)
    print(f"total: {total} Knowledge object(s) recorded")
    return 0


# --- subcommands: K3 consolidation --------------------------------------------


def cmd_consolidate(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    backend = _build_backend(args)
    candidates = _select_cases(ledger, args)
    if len(candidates) < 2:
        print(f"only {len(candidates)} candidate(s) found; need at least 2 to consolidate", file=sys.stderr)
        return 1
    memory = asyncio.run(Consolidator(ledger, backend).consolidate(candidates, about=args.about, force=args.force))
    _print_rev(memory, as_json=args.json)
    return 0


# --- subcommands: K4 principles -----------------------------------------------


def cmd_extract_principle(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    backend = _build_backend(args)
    cases = _select_cases(ledger, args)
    if len(cases) < 2:
        print(f"only {len(cases)} case(s) found; need at least 2 to extract a principle", file=sys.stderr)
        return 1
    principle = asyncio.run(PrincipleExtractor(ledger, backend).extract(cases, subject=args.about))
    _print_rev(principle, as_json=args.json)
    return 0


def cmd_counterexample(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    backend = _build_backend(args)
    principle = ledger.head(args.principle_id)
    if principle is None:
        raise ValueError(f"unknown knowledge_id: {args.principle_id}")
    pool = _select_cases(ledger, args)
    found = asyncio.run(CounterexampleSearcher(backend).search(principle, pool))
    if not found:
        print("no counterexample found")
        return 0
    for case in found:
        print(_fmt_line(case))
    return 0


def cmd_refine(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    principle = ledger.head(args.principle_id)
    if principle is None:
        raise ValueError(f"unknown knowledge_id: {args.principle_id}")
    counterexamples = [ledger.head(i) for i in args.counterexample_ids]
    missing = [i for i, c in zip(args.counterexample_ids, counterexamples) if c is None]
    if missing:
        raise ValueError(f"unknown knowledge_id(s): {', '.join(missing)}")
    refined = refine_principle(ledger, principle, counterexamples, refined_text=args.text)
    _print_rev(refined, as_json=args.json)
    return 0


def cmd_support(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    principle = ledger.head(args.principle_id)
    if principle is None:
        raise ValueError(f"unknown knowledge_id: {args.principle_id}")
    updated = record_support(ledger, principle, args.evidence_id)
    _print_rev(updated, as_json=args.json)
    return 0


def cmd_predict(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    backend = _build_backend(args)
    principle = ledger.head(args.principle_id)
    if principle is None:
        raise ValueError(f"unknown knowledge_id: {args.principle_id}")
    view = WorldStateProjection(ledger).view(as_of=_parse_dt(args.as_of))
    prediction = asyncio.run(PredictionEngine(ledger, backend).predict(principle, view, subject=args.subject))
    _print_rev(prediction, as_json=args.json)
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    prediction = ledger.head(args.prediction_id)
    if prediction is None:
        raise ValueError(f"unknown knowledge_id: {args.prediction_id}")
    actual = json.loads(args.actual)
    updated, matched = evaluate_prediction(ledger, prediction, actual)
    print(f"matched: {matched}")
    _print_rev(updated, as_json=args.json)
    if args.principle_id:
        principle = ledger.head(args.principle_id)
        if principle is None:
            raise ValueError(f"unknown knowledge_id: {args.principle_id}")
        updated_principle = apply_prediction_feedback(ledger, principle, updated, matched)
        print("--- principle after feedback ---")
        _print_rev(updated_principle, as_json=args.json)
    return 0


# --- subcommands: K5 goal integration -----------------------------------------


def _print_signal(signal: Signal) -> None:
    principle = f" (principle {signal.principle_id})" if signal.principle_id else ""
    print(f"[{signal.type}] {signal.description}{principle}")
    print(f"    confidence={signal.confidence}  request={signal.request!r}")


def cmd_signals(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    backend = _build_backend(args)
    principles = _select_principles(ledger, args.principle_ids)
    view = WorldStateProjection(ledger).view()
    signals = asyncio.run(GapRiskOpportunityDetector(backend).detect(view, principles, intentions=args.intention))
    if not signals:
        print("no signals detected")
        return 0
    for signal in signals:
        _print_signal(signal)
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    """Detect signals and file them as proposals for the loop to route.

    Filing is where this stops: the autonomy policy decides whether a proposal
    proceeds automatically or waits for a person, and ``reconcile`` is what
    submits an approved one to the Project Orchestrator.
    """
    ledger = _open_ledger(args.db)
    principles = _select_principles(ledger, args.principle_ids)
    view = WorldStateProjection(ledger).view()
    signals = asyncio.run(
        GapRiskOpportunityDetector(_build_backend(args)).detect(
            view, principles, intentions=args.intention
        )
    )
    if not signals:
        print("no signals detected; nothing proposed")
        return 0

    filed = asyncio.run(
        GoalBridge(ledger, min_confidence=args.min_confidence).submit(
            signals, source=args.source
        )
    )
    if not filed:
        print(
            f"{len(signals)} signal(s) detected; none filed "
            f"(below --min-confidence {args.min_confidence}, or already proposed)"
        )
        return 0
    for signal, proposal in filed:
        _print_signal(signal)
        print(f"    -> {proposal.status}  {proposal.knowledge_id}")
    print("run `reconcile` to route whatever the policy approved")
    return 0


# --- subcommands: K6 self-learning --------------------------------------------


# --- subcommands: the autonomous loop -----------------------------------------


def _open_loop(args: argparse.Namespace):
    """Open the same loop the application runs, on the same database.

    Returns ``(runtime, loop)``; the caller closes the runtime.  Every decision
    below goes through :class:`KnowledgeLoop`, the same object the Cockpit
    calls, so the CLI is another way in rather than a second implementation.
    """
    from .knowledge.autonomous_loop import KnowledgeLoop
    from .orchestrator import InProcessAgentRuntime, ProjectOrchestrator
    from .orchestrator_config import ProjectAgentSettings, build_grant_policy
    from .runtime.runtime import Runtime

    runtime = Runtime(args.db)
    # The bootstrap context lives beside the database, which is where the
    # application puts it, so the CLI reads the same three files a running
    # NEXUS SEED does rather than a second copy.
    context_root = getattr(args, "context", None) or (
        Path(args.db).expanduser().resolve().parent / CONTEXT_DIR
    )
    # The same workspace root and authorized roots the application runs with,
    # so a grant made from the command line lands where the Agent looks and is
    # judged against the roots an operator actually authorized.
    settings = ProjectAgentSettings.from_env(getattr(args, "env_file", ".env") or ".env")
    orchestrator = ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(),
        backend=_build_backend(args),
        workspace_root=settings.workspace_root,
        grant_policy=build_grant_policy(settings),
    )
    loop = KnowledgeLoop(
        runtime,
        orchestrator,
        backend=_build_backend(args),
        context_root=context_root,
    )
    runtime.knowledge_loop = loop
    return runtime, loop


#: Kinds that can be waiting on a person, and the status that means "waiting".
_PENDING = {
    "project_proposal": "PENDING_REVIEW",
    "artifact": "PENDING_REVIEW",
    "completion_review": "PENDING_REVIEW",
    "question": "OPEN",
    "entity_candidate": "UNRESOLVED",
}


def cmd_pending(args: argparse.Namespace) -> int:
    """Everything the loop is waiting for a person to decide."""
    runtime, loop = _open_loop(args)
    try:
        waiting = [
            item
            for group in (
                loop.proposals(),
                loop.artifacts(),
                loop.completion_reviews(),
                loop.questions(open_only=True),
                loop.entity_candidates(unresolved_only=True),
            )
            for item in group
            if item.status == _PENDING.get(item.kind)
        ]
        if args.json:
            print(json.dumps([item.to_dict() for item in waiting], ensure_ascii=False, indent=2))
        else:
            for item in waiting:
                print(_fmt_line(item))
            print(f"({len(waiting)} waiting for a decision)")
    finally:
        runtime.close()
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    """Approve or reject whatever is waiting under this knowledge id.

    The kind decides which decision path runs, so a caller does not have to
    remember whether an id is a proposal, an artifact or a completion.
    """
    runtime, loop = _open_loop(args)
    try:
        item = loop.ledger.head(args.knowledge_id)
        if item is None:
            print(f"error: unknown knowledge_id: {args.knowledge_id}", file=sys.stderr)
            return 1
        decision = args.decision
        if item.kind == "project_proposal":
            result = asyncio.run(loop.decide_proposal(args.knowledge_id, decision, note=args.note))
        elif item.kind == "artifact":
            result = asyncio.run(loop.decide_artifact(args.knowledge_id, decision, note=args.note))
        elif item.kind == "completion_review":
            result = asyncio.run(
                loop.decide_completion_review(args.knowledge_id, decision, note=args.note)
            )
        elif item.kind == "entity_candidate":
            result = loop.decide_entity(
                args.knowledge_id,
                "confirm" if decision == "approve" else "separate",
                canonical_id=args.canonical_id,
            )
        else:
            print(
                f"error: {item.kind} does not take an approve/reject decision",
                file=sys.stderr,
            )
            return 1
        if result is None:
            print("error: nothing to decide under that id", file=sys.stderr)
            return 1
        _print_rev(result, as_json=args.json)
    finally:
        runtime.close()
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    """Show — or take a fresh reading of — what a person wrote in context/."""
    runtime, loop = _open_loop(args)
    try:
        source = loop.context_documents
        created = []
        if not source.root.exists():
            # No directory at all means nobody has started: make one with a
            # prompt in each file rather than reporting that it is missing.
            created = source.ensure()
            print(f"created {source.root} with an empty terms/goals/situation")
        changed = source.sync() if args.sync else []
        documents = loop.context()
        if args.json:
            print(
                json.dumps(
                    {
                        "root": str(source.root),
                        "created": [str(path) for path in created],
                        "changed": [item.knowledge_id for item in changed],
                        "documents": documents,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        print(f"context root: {source.root}")
        if not documents:
            print("(context/ にはまだ何も書かれていません)")
            return 0
        for role, document in documents.items():
            marker = " *changed*" if any(
                item.knowledge_id == document["knowledge_id"] for item in changed
            ) else ""
            print(f"\n--- {role}.md (rev {document['revision']}){marker} ---")
            print(str(document["text"]).rstrip())
    finally:
        runtime.close()
    return 0


def cmd_assess(args: argparse.Namespace) -> int:
    """Read the bootstrap context once against the current world.

    Nothing is executed: what comes out is findings and suggestions, and a
    suggestion waits for ``candidate ... approve``.
    """
    runtime, loop = _open_loop(args)
    try:
        loop.context_documents.sync()
        recorded = asyncio.run(loop.context_assessor.assess())
        if recorded is None:
            print(
                "no assessment: nothing changed since the last one, "
                "context/ is empty, or no LLM is configured (see --llm)"
            )
            return 0
        if args.json:
            _print_rev(recorded, as_json=True)
            return 0
        findings = recorded.content.value
        for kind in FINDING_KINDS:
            items = findings.get(kind) or []
            if not items:
                continue
            print(f"{kind}:")
            for item in items:
                print(f"    {_preview(item.get('description'), 90)}")
                for evidence in item.get("evidence") or []:
                    print(f"        evidence: {_preview(evidence, 80)}")
        pending = loop.task_candidates(status=CANDIDATE_PENDING_REVIEW)
        print(f"\n({len(pending)} task candidate(s) waiting for a decision)")
    finally:
        runtime.close()
    return 0


def cmd_candidates(args: argparse.Namespace) -> int:
    """List suggested Tasks and what each is standing on."""
    runtime, loop = _open_loop(args)
    try:
        items = loop.task_candidates(status=args.status)
        if args.json:
            print(json.dumps([item.to_dict() for item in items], ensure_ascii=False, indent=2))
            return 0
        for item in items:
            metadata = item.metadata
            print(
                f"[{item.status}] {item.knowledge_id}  "
                f"({metadata.get('suggested_assignee_type', 'UNKNOWN')}, "
                f"confidence={metadata.get('confidence', '?')})"
            )
            print(f"    {_preview(item.content.value, 90)}")
            if metadata.get("reason"):
                print(f"    reason: {_preview(metadata['reason'], 80)}")
            if metadata.get("project_id"):
                print(f"    project: {metadata['project_id']}")
        print(f"({len(items)} candidate(s))")
    finally:
        runtime.close()
    return 0


def cmd_candidate(args: argparse.Namespace) -> int:
    """Run, ignore, or reword one suggested Task."""
    runtime, loop = _open_loop(args)
    try:
        result = asyncio.run(
            loop.decide_task_candidate(
                args.knowledge_id,
                args.decision,
                note=args.note,
                description=args.description,
            )
        )
        if result is None:
            print(f"error: no task candidate under {args.knowledge_id}", file=sys.stderr)
            return 1
        _print_rev(result, as_json=args.json)
    finally:
        runtime.close()
    return 0


def cmd_grants(args: argparse.Namespace) -> int:
    """What Agents are asking to be given, and what they already have.

    Read straight from the Projects rather than a separate queue: a
    ``NEED_RESOURCE`` escalation already *is* the request, so this is a view
    of live state and never disagrees with it.
    """
    runtime, loop = _open_loop(args)
    try:
        projects = (
            [loop.orchestrator.projects.get(args.project_id)]
            if args.project_id
            else loop.orchestrator.projects.all()
        )
        rows = []
        for project in projects:
            if project is None:
                raise ValueError(f"unknown project_id: {args.project_id}")
            requests = loop.orchestrator.grant_requests(project.id)
            granted = loop.orchestrator.granted(project.id)
            if not requests and not granted and args.project_id is None:
                continue
            rows.append(
                {
                    "project_id": project.id,
                    "goal": project.goal,
                    "status": project.status.value,
                    "requested": [item.to_dict() for item in requests],
                    "granted": [item.to_dict() for item in granted],
                }
            )
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("no project has asked for or been given a resource")
            return 0
        for row in rows:
            print(f"[{row['status']}] {row['project_id']}  {_preview(row['goal'], 60)}")
            for item in row["granted"]:
                print(f"    granted   {item['access']:10} {item['uri']}")
            for item in row["requested"]:
                uri = item.get("uri") or "(no uri - a person has to say which resource)"
                print(f"    REQUESTED {item['access']:10} {uri}")
                if item.get("requested"):
                    print(f"              asked for: {_preview(item['requested'], 70)}")
                if item.get("reason"):
                    print(f"              reason:    {_preview(item['reason'], 70)}")
    finally:
        runtime.close()
    return 0


def cmd_grant(args: argparse.Namespace) -> int:
    """Give one Project one resource, and let the same Task continue.

    Nothing about the task restarts: an allowed grant rebuilds the workspace
    with the resource in it and re-delegates the *same* Project.
    """
    runtime, loop = _open_loop(args)
    try:
        decision = asyncio.run(
            loop.orchestrator.grant_resource(
                args.project_id,
                args.uri,
                access=args.access,
                reason=args.reason,
                name=args.name or "",
                resume=not args.no_resume,
            )
        )
        if args.json:
            print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
        elif decision.allowed:
            print(f"granted: {decision.reason}")
        else:
            print(f"refused: {decision.reason}", file=sys.stderr)
        return 0 if decision.allowed else 1
    finally:
        runtime.close()


def cmd_collect(args: argparse.Namespace) -> int:
    """Carry a finished task's writable grants back to the files they came from."""
    runtime, loop = _open_loop(args)
    try:
        written = loop.orchestrator.collect_workspace(args.project_id)
        if args.json:
            print(json.dumps(written, ensure_ascii=False, indent=2))
        elif written:
            for uri in written:
                print(f"written back: {uri}")
        else:
            print("nothing to carry back (no writable grant was changed)")
    finally:
        runtime.close()
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    runtime, loop = _open_loop(args)
    try:
        result = asyncio.run(loop.answer_question(args.knowledge_id, args.answer))
        if result is None:
            print(f"error: no open question under {args.knowledge_id}", file=sys.stderr)
            return 1
        _print_rev(result, as_json=args.json)
    finally:
        runtime.close()
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Run one bounded pass of the loop and report what it changed."""
    runtime, loop = _open_loop(args)
    try:
        result = asyncio.run(loop.reconcile())
        counts = {
            field: getattr(result, field)
            for field in (
                "manual_observations", "source_observations", "resource_observations",
                "agent_reports", "consolidations", "principles", "proposals",
                "projects_routed", "knowledge_events", "completion_decisions_reconciled",
                "context_documents", "context_assessments", "task_candidates_routed",
            )
        }
        if args.json:
            print(json.dumps({**counts, "errors": result.errors}, ensure_ascii=False, indent=2))
        else:
            for name, value in counts.items():
                if value:
                    print(f"{name}: {value}")
            if not any(counts.values()):
                print("(nothing changed)")
            for error in result.errors:
                print(f"error: {error}", file=sys.stderr)
        return 1 if result.errors else 0
    finally:
        runtime.close()


def cmd_watch_folder(args: argparse.Namespace) -> int:
    """Authorize a folder as a standing observation of how much is piling up.

    Distinct from the file observer, which reports that a file *changed*:
    this reports the folder's situation — how many, how large, how old — which
    is a fact about the world even on a day when nothing changed.  Only
    directory metadata is read; no file is opened.
    """
    from .observation_sources import DEFAULT_FOLDER_FIELDS, ObservationSourceService
    from .runtime.runtime import Runtime

    runtime = Runtime(args.db)
    try:
        service = ObservationSourceService(runtime, data_root=args.data_root or ".")
        source = service.create_folder_status(
            name=args.name,
            path=args.path,
            fields=args.fields or list(DEFAULT_FOLDER_FIELDS),
            poll_interval_seconds=args.interval,
            recursive=not args.no_recursive,
        )
        outcomes = asyncio.run(service.poll_due(force_source_id=source.id))
        if args.json:
            print(json.dumps(
                {"source": source.to_dict(), "outcomes": outcomes},
                ensure_ascii=False, indent=2,
            ))
        else:
            print(f"{source.id}  {source.name}  {source.config['path']}")
            print(f"    fields: {', '.join(source.fields)}")
            for outcome in outcomes:
                print(f"    first reading: {outcome.get('status')}")
    finally:
        runtime.close()
    return 0


def cmd_principles(args: argparse.Namespace) -> int:
    """What the loop has generalised from experience, and how well it holds."""
    ledger = _open_ledger(args.db)
    from .knowledge.models import KIND_PRINCIPLE

    items = sorted(
        ledger.by_kind(KIND_PRINCIPLE), key=lambda i: i.recorded_at, reverse=True
    )
    if args.mature_only:
        items = [i for i in items if i.status in (STATUS_SUPPORTED, STATUS_VALIDATED)]
    if args.json:
        print(json.dumps([i.to_dict() for i in items], ensure_ascii=False, indent=2))
        return 0
    for item in items:
        meta = item.metadata or {}
        print(f"{item.knowledge_id}  [{item.status}]  "
              f"支持 {meta.get('support_count', 0)} / 反例 {meta.get('counterexample_count', 0)}")
        print(f"    {_preview(item.content.value)}")
    print(f"({len(items)} principles)")
    return 0


def cmd_memories(args: argparse.Namespace) -> int:
    """Consolidated memories, and what each one compressed."""
    ledger = _open_ledger(args.db)
    from .knowledge.models import KIND_CONSOLIDATED_MEMORY

    items = sorted(
        ledger.by_kind(KIND_CONSOLIDATED_MEMORY),
        key=lambda i: i.recorded_at,
        reverse=True,
    )
    if args.json:
        print(json.dumps([i.to_dict() for i in items], ensure_ascii=False, indent=2))
        return 0
    for item in items:
        meta = item.metadata or {}
        about = meta.get("about") or "(no subject)"
        print(f"{item.knowledge_id}  about={about}  "
              f"{len(item.derived_from)} sources  gen={meta.get('generation', '?')}")
        print(f"    {_preview(item.content.value)}")
        if meta.get("unresolved"):
            print(f"    unresolved: {', '.join(meta['unresolved'])}")
    print(f"({len(items)} memories)")
    return 0


def cmd_experience(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    attempts = json.loads(args.attempts_json) if args.attempts_json else None
    switches = json.loads(args.switches_json) if args.switches_json else None
    rev = record_agent_experience(
        ledger,
        agent_id=args.agent_id,
        task=args.task,
        outcome=args.outcome,
        attempts=attempts,
        strategy_switches=switches,
        source_ref=args.source_ref,
    )
    _print_rev(rev, as_json=args.json)
    return 0


def cmd_advise(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.db)
    principles = _select_principles(ledger, args.principle_ids)
    advisories = advisories_for(principles, subject=args.subject)
    if not advisories:
        print("no mature (supported/validated) principle advisories available")
        return 0
    for advisory in advisories:
        print(f"[{advisory.status}] ({advisory.principle_id}, confidence={advisory.confidence})")
        print(f"    {advisory.recommendation}")
    return 0


# --- argument parser -----------------------------------------------------------


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, help="path to the shared SQLite database")


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print full JSON instead of a summary")


def _add_backend_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--llm", action="store_true", help="use the real LLM backend even if NEXUS_SEED_LLM_ENABLED is not set")
    group.add_argument("--no-llm", action="store_true", help="never call an LLM; always use the deterministic fallback")
    parser.add_argument("--env-file", default=".env", help="env file to read NEXUS_SEED_LLM_* from (default: .env)")


def _add_case_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--about", default=None, help="select Knowledge related (`about`) to this subject")
    parser.add_argument("--ids", nargs="*", default=None, help="explicit knowledge_ids instead of --about")
    parser.add_argument("--kind", action="append", default=None, help="restrict to this kind (repeatable)")
    parser.add_argument("--max-items", type=int, default=20, help="cap on how many candidates are selected")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus-seed-knowledge",
        description="Operate the Knowledge Runtime (nexus_seed.knowledge) from the command line.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record", help="record a new raw Knowledge object")
    _add_db(p); _add_json(p)
    p.add_argument("--content", default=None, help="the text to record (or use --content-file / stdin)")
    p.add_argument("--content-file", default=None, help="read content from this file")
    p.add_argument("--source-type", required=True, help='e.g. "meeting", "report", "chat"')
    p.add_argument("--source-ref", default=None)
    p.add_argument("--kind", default="raw")
    p.add_argument("--knowledge-id", default=None, help="fixed id instead of a generated one")
    p.add_argument("--valid-from", default=None, help="ISO timestamp: when this became true in reality")
    p.add_argument("--valid-to", default=None)
    p.add_argument("--about", default=None, help="attach an `about` relation to this subject")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("revise", help="append a new revision of an existing Knowledge object")
    _add_db(p); _add_json(p)
    p.add_argument("knowledge_id")
    p.add_argument("--content", default=None)
    p.add_argument("--content-file", default=None)
    p.add_argument("--valid-from", default=None)
    p.add_argument("--valid-to", default=None)
    p.add_argument("--status", default=None)
    p.add_argument("--reason", default=None)
    p.set_defaults(func=cmd_revise)

    p = sub.add_parser("relate", help="attach a relation to another Knowledge object or entity")
    _add_db(p); _add_json(p)
    p.add_argument("knowledge_id")
    p.add_argument("--type", required=True, help='e.g. "about", "supports", "contradicts"')
    p.add_argument("--target", required=True)
    p.set_defaults(func=cmd_relate)

    p = sub.add_parser("conflict", help="mark two Knowledge objects as contradicting each other")
    _add_db(p)
    p.add_argument("knowledge_id_a")
    p.add_argument("knowledge_id_b")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_conflict)

    p = sub.add_parser("fact", help="attach a world_fact annotation (opt into the World View)")
    _add_db(p); _add_json(p)
    p.add_argument("knowledge_id")
    p.add_argument("--entity", required=True)
    p.add_argument("--attribute", required=True)
    p.add_argument("--value", required=True, help="JSON if it parses, else a plain string")
    p.add_argument("--confidence", type=float, default=None)
    p.set_defaults(func=cmd_fact)

    p = sub.add_parser("show", help="show one Knowledge object (HEAD or full history)")
    _add_db(p); _add_json(p)
    p.add_argument("knowledge_id")
    p.add_argument("--history", action="store_true", help="show every revision instead of just HEAD")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("list", help="list Knowledge heads (or every revision), filtered")
    _add_db(p); _add_json(p)
    p.add_argument("--kind", default=None)
    p.add_argument("--status", default=None)
    p.add_argument("--source-type", default=None)
    p.add_argument("--about", default=None)
    p.add_argument("--all-revisions", action="store_true", help="include every revision, not just HEADs")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("view", help="project the current (or a past) World View")
    _add_db(p); _add_json(p)
    p.add_argument("--as-of", default=None, help="ISO timestamp: what we believed by this transaction time")
    p.add_argument("--valid-at", default=None, help="ISO timestamp: what was true at this real-world time")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("diff", help="diff two World Views, optionally emitting state_changed Events")
    _add_db(p)
    p.add_argument("--before", default=None, help="ISO transaction time for the 'before' view")
    p.add_argument("--before-valid-at", default=None, help="ISO valid time for the 'before' view")
    p.add_argument("--after", default=None, help="ISO transaction time for the 'after' view (default: now)")
    p.add_argument("--after-valid-at", default=None)
    p.add_argument("--emit-events", action="store_true", help="append each change as a state_changed Event")
    p.add_argument("--event-source", default="knowledge_runtime_cli")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("pptx", help="record each slide of local .pptx file(s) as raw Knowledge")
    _add_db(p)
    p.add_argument("files", nargs="*")
    p.add_argument("--dir", default=None, help="also ingest every .pptx under this directory (recursive)")
    p.add_argument("--about", default=None, help="relation target shared by every file (default: each file's own name)")
    p.set_defaults(func=cmd_pptx)

    p = sub.add_parser("consolidate", help="consolidate related Knowledge into one memory")
    _add_db(p); _add_json(p); _add_backend_args(p); _add_case_selection(p)
    p.add_argument("--force", action="store_true", help="re-consolidate even if a fresh result already exists")
    p.set_defaults(func=cmd_consolidate)

    p = sub.add_parser("extract-principle", help="generalise >=2 related cases into a candidate principle")
    _add_db(p); _add_json(p); _add_backend_args(p); _add_case_selection(p)
    p.set_defaults(func=cmd_extract_principle)

    p = sub.add_parser("counterexample", help="search a pool of cases for one that contradicts a principle")
    _add_db(p); _add_backend_args(p); _add_case_selection(p)
    p.add_argument("--principle-id", required=True)
    p.set_defaults(func=cmd_counterexample)

    p = sub.add_parser("refine", help="narrow a principle's scope after a counterexample")
    _add_db(p); _add_json(p)
    p.add_argument("--principle-id", required=True)
    p.add_argument("--counterexample-ids", nargs="+", required=True)
    p.add_argument("--text", default=None, help="the refined principle text (default: an auto-generated caveat)")
    p.set_defaults(func=cmd_refine)

    p = sub.add_parser("support", help="record one more independent confirmation of a principle")
    _add_db(p); _add_json(p)
    p.add_argument("--principle-id", required=True)
    p.add_argument("--evidence-id", required=True, help="a knowledge_id (or any identifier) for the evidence")
    p.set_defaults(func=cmd_support)

    p = sub.add_parser("predict", help="apply a principle to the current World View")
    _add_db(p); _add_json(p); _add_backend_args(p)
    p.add_argument("--principle-id", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--as-of", default=None)
    p.set_defaults(func=cmd_predict)

    p = sub.add_parser("evaluate", help="score a structured prediction against an actual outcome")
    _add_db(p); _add_json(p)
    p.add_argument("--prediction-id", required=True)
    p.add_argument("--actual", required=True, help='JSON, e.g. \'{"project-A": {"risk": "high"}}\'')
    p.add_argument("--principle-id", default=None, help="also apply the result back onto this principle")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("signals", help="detect Gap/Risk/Opportunity/Conflict, without submitting anything")
    _add_db(p); _add_backend_args(p)
    p.add_argument("--principle-ids", nargs="*", default=None, help="default: every supported/validated principle")
    p.add_argument("--intention", action="append", default=None)
    p.set_defaults(func=cmd_signals)

    p = sub.add_parser(
        "propose",
        help="detect signals and file them as project proposals (the loop routes them)",
    )
    _add_db(p); _add_backend_args(p)
    p.add_argument("--principle-ids", nargs="*", default=None)
    p.add_argument("--intention", action="append", default=None)
    p.add_argument("--source", default="knowledge_runtime_cli")
    p.add_argument("--min-confidence", type=float, default=0.5)
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("experience", help="record one Agent execution episode as Experience Knowledge")
    _add_db(p); _add_json(p)
    p.add_argument("--agent-id", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--outcome", required=True)
    p.add_argument("--attempts-json", default=None, help='JSON list, e.g. \'[{"tool": "x", "error": "..."}]\'')
    p.add_argument("--switches-json", default=None, help='JSON list, e.g. \'[{"from": "a", "to": "b", "reason": "..."}]\'')
    p.add_argument("--source-ref", default=None)
    p.set_defaults(func=cmd_experience)

    p = sub.add_parser(
        "watch-folder",
        help="authorize a folder as a standing observation of what is piling up",
    )
    _add_db(p); _add_json(p)
    p.add_argument("path", help="absolute path to the folder to size up")
    p.add_argument("--name", required=True, help="what to call this observation source")
    p.add_argument("--fields", nargs="*", default=None,
                   help="file_count / total_bytes / oldest_change / newest_change / by_extension")
    p.add_argument("--interval", type=float, default=300.0, help="seconds between readings")
    p.add_argument("--no-recursive", action="store_true", help="do not descend into subfolders")
    p.add_argument("--data-root", default=None, help="NEXUS_SEED_DATA_DIR (unused by this kind)")
    p.set_defaults(func=cmd_watch_folder)

    p = sub.add_parser("principles", help="what the loop generalised, and how well each holds up")
    _add_db(p); _add_json(p)
    p.add_argument("--mature-only", action="store_true", help="only supported/validated")
    p.set_defaults(func=cmd_principles)

    p = sub.add_parser("memories", help="consolidated memories and what each compressed")
    _add_db(p); _add_json(p)
    p.set_defaults(func=cmd_memories)

    p = sub.add_parser("reconcile", help="run one bounded pass of the autonomous loop")
    _add_db(p); _add_json(p); _add_backend_args(p)
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("pending", help="everything the loop is waiting for a person to decide")
    _add_db(p); _add_json(p); _add_backend_args(p)
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("decide", help="approve or reject a proposal, artifact, completion or entity")
    _add_db(p); _add_json(p); _add_backend_args(p)
    p.add_argument("knowledge_id")
    p.add_argument("decision", choices=["approve", "reject"])
    p.add_argument("--note", default="", help="reason, recorded with the decision")
    p.add_argument("--canonical-id", default=None, help="entity only: the id it is the same as")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("answer", help="answer an Agent's open question")
    _add_db(p); _add_json(p); _add_backend_args(p)
    p.add_argument("knowledge_id")
    p.add_argument("answer")
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("context", help="show the bootstrap context (terms/goals/situation)")
    _add_db(p); _add_json(p); _add_backend_args(p)
    p.add_argument("--context", default=None, help="context directory (default: <db dir>/context)")
    p.add_argument("--sync", action="store_true", help="record any document that changed on disk first")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("assess", help="read the bootstrap context against the current world")
    _add_db(p); _add_json(p); _add_backend_args(p)
    p.add_argument("--context", default=None, help="context directory (default: <db dir>/context)")
    p.set_defaults(func=cmd_assess)

    p = sub.add_parser("candidates", help="suggested Tasks waiting for a person")
    _add_db(p); _add_json(p); _add_backend_args(p)
    p.add_argument("--context", default=None, help="context directory (default: <db dir>/context)")
    p.add_argument("--status", default=None, help="only this status (default: every candidate)")
    p.set_defaults(func=cmd_candidates)

    p = sub.add_parser("candidate", help="run, ignore, or reword one suggested Task")
    _add_db(p); _add_json(p); _add_backend_args(p)
    p.add_argument("--context", default=None, help="context directory (default: <db dir>/context)")
    p.add_argument("knowledge_id")
    p.add_argument("decision", choices=["approve", "reject", "amend"])
    p.add_argument("--description", default=None, help="the new wording (required with amend)")
    p.add_argument("--note", default="", help="why (kept with the decision)")
    p.set_defaults(func=cmd_candidate)

    p = sub.add_parser("grants", help="what Agents are asking to be given, and what they already have")
    _add_db(p); _add_json(p); _add_backend_args(p)
    p.add_argument("--project-id", default=None, help="only this project (default: every project with a request or a grant)")
    p.set_defaults(func=cmd_grants)

    p = sub.add_parser("grant", help="give one Project one resource and continue the same Task")
    _add_db(p); _add_json(p); _add_backend_args(p)
    p.add_argument("project_id")
    p.add_argument("uri", help="file:<path>, resource:<uri> or knowledge:<id>")
    p.add_argument("--access", default="read", choices=["read", "read_write"], help="default: read")
    p.add_argument("--reason", default="", help="why this is being granted (kept with the grant)")
    p.add_argument("--name", default=None, help="name to give it inside the workspace")
    p.add_argument("--no-resume", action="store_true", help="record the grant without re-delegating the Task")
    p.set_defaults(func=cmd_grant)

    p = sub.add_parser("collect", help="carry a finished task's writable grants back to their originals")
    _add_db(p); _add_json(p); _add_backend_args(p)
    p.add_argument("project_id")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("advise", help="list mature (supported/validated) principles as plain advisories")
    _add_db(p)
    p.add_argument("--principle-ids", nargs="*", default=None)
    p.add_argument("--subject", default=None, help="only principles related (`about`) to this subject")
    p.set_defaults(func=cmd_advise)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        # Domain errors from nexus_seed.knowledge (unknown knowledge_id passed
        # straight through, a prediction with no structured predicted_state,
        # malformed --actual/--attempts-json JSON, a missing --content-file,
        # ...) are user mistakes, not bugs - report them plainly rather than
        # a Python traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
