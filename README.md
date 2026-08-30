# NEXUS SEED

NEXUS SEED is a durable **Project Orchestrator**. It decides which Projects
exist, assigns one Agent to each Project, and handles the Agent's results and
escalations.

```text
ContextManager -> ProjectRouter -> ProjectManager -> AgentManager -> A2AGateway
```

NEXUS SEED owns project identity, priority, lifecycle, assignment, blockers,
and the audited Agent channel. The Agent owns task decomposition, capability
selection, tools, and execution.

*[日本語版](README.ja.md)*

## Quick start

The complete operator guide is in [README.ja.md](README.ja.md#クイックスタート).
The shortest local setup is:

```bash
uv sync --extra dev
cp .env.example .env
# Set NEXUS_SEED_DATA_DIR to a directory outside this repository.
uv run nexus-seed config
uv run nexus-seed
```

Open `http://127.0.0.1:8787/cockpit`. Use **World** to register an explicit
System Snapshot source, record observations, inspect provenance, and settle
Proposal, Artifact, Completion, Question, and Entity reviews. Use **Projects**
to inspect assignments, blockers, the A2A audit trail, and to send follow-up
instructions to the same Project Agent.

```bash
uv run nexus-seed task "Analyse resources/sales.csv and explain the decline"
uv run nexus-seed status
```

NEXUS SEED never creates a PC observation source implicitly. A source can read
only the selected fixed fields: platform, hostname, logical CPU count, physical
memory, and disk usage for `NEXUS_SEED_DATA_DIR`. Files are watched only under
`NEXUS_SEED_DATA_DIR/resources`. Configuration, checkpoints, Knowledge,
Projects, and review decisions survive restart in SQLite.

## Current status

The Project Orchestrator is available as a Python API and includes:

- durable SQLite records for Projects, Agents, and A2A messages;
- semantic routing to create a Project, add a task, update a Project, or ignore
  a request;
- the invariant **one Project = one Agent**;
- audited handling of completion, status, blockers, human-input requests, and
  newly discovered Projects;
- restart-safe Project, Agent and hand-over state, reconciled on start;
- a deterministic `InProcessAgentRuntime` for tests and local integration;
- `A2AAgentRuntime`, which delegates a whole Project to a real external Agent
  over A2A;
- ordinary requests — `nexus-seed task`, a webhook, any connector — always
  routed into Projects;
- authorized, per-resource delegation: a Project declares the files and
  directories its Agent may read or change, decided by deterministic policy
  against configured roots and handed over as the Agent runtime's own
  `readable_paths` / `writable_paths` — read is a link to the original, write
  is a copy whose edits arrive only when the work is accepted;
- a prose bootstrap context (terms / goals / situation) read against the live
  world into contradictions, gaps and suggested Tasks, each waiting for a
  person to run, ignore or reword it; and
- a Cockpit **Projects** view over the orchestrator's own records.

What is deliberately not built yet: several Agents on one Project, and Agents
talking to each other.

The former Action, Work, Capability, Planning, and self-extension pipelines
were removed after those responsibilities moved to the Project Agent. Durable
Event delivery, Process resume, Ingress, Resources, and Context remain because
the Orchestrator and Knowledge loop use them. Retired tables in an existing
SQLite database are preserved as history, but are not created in a new one.

## Minimal MVP loop

For the small, explicitly approved Observer-to-Project loop, use the
replaceable `nexus_seed.mvp` application layer. It reuses the existing durable
Knowledge Ledger, Project records, and LLM backend through adapters without
coupling the MVP flow to the full orchestration runtime. See
[MVP architecture and usage](docs/mvp.md).

```bash
uv run nexus-seed-mvp "Improve the NEXUS SEED README" --workspace .
```

## Knowledge Runtime

Above the Project Orchestrator sits a Knowledge Runtime (`nexus_seed/knowledge/`)
that keeps a versioned, provenance-bearing record of everything NEXUS SEED has
been told, and derives the Orchestrator's world view from it:

```text
Knowledge Runtime      "how does NEXUS SEED perceive the world?"
Project Orchestrator   "what should be done about it?"
Agent Runtime          "how does it get done?"
```

- an append-only Knowledge Ledger — nothing is ever overwritten or deleted; a
  correction, an annotation, or a relation is always a new revision, queryable
  by transaction time or valid time (including late-arriving evidence);
- a World Projection built from the Ledger, kept separate from — and
  read-compatible with — the existing World State API;
- Memory Consolidation that compresses related Knowledge into a
  `consolidated_memory` object without deleting its sources or resolving a
  contradiction it cannot actually resolve;
- Principle Extraction that generalises several cases into a candidate
  principle, tests it against counterexamples, and evaluates it as a
  predictor against actual outcomes;
- a Goal bridge that turns a detected Gap/Risk/Opportunity into a project
  proposal, judged by the same autonomy policy as any other — it never
  creates a Project directly;
- a bootstrap context a person writes in prose — what the words mean, what a
  good state looks like, what is true now — read against the live world to
  find contradictions, gaps and things nobody knows yet ([below](#the-context-you-write-yourself));
- operator-authorized observation sources: the machine's own fixed fields,
  and a folder's standing situation (how many files, how large, how old the
  oldest is) — read from directory metadata only, never by opening a file.

It is usable as a library (`nexus_seed.knowledge`), from the command line
(`nexus-seed-knowledge`, below), and as the application's autonomous Knowledge
loop. Authorized resources, explicit System Snapshot sources, and manual
Cockpit observations become Knowledge; approved work is routed through the
Project Orchestrator, and Agent results return to Knowledge. See
[Architecture and phase history](docs/architecture.md) for the full design and
[AGENTS.md](AGENTS.md) for the invariants it keeps.

Each pass of the loop re-reads before it decides:

```text
evidence -> consolidate -> extract a principle -> assess -> propose -> Project
```

- **Consolidation is lazy and grouped.** A subject is compressed once enough
  observations about it are not yet covered by any memory, at most one or two
  subjects per pass. Knowledge that names no subject is compressed too, under
  one shared bucket, rather than piling up unread. A memory is then evidence
  in its own right — that is what makes compressing worth doing.
- **A principle generalises over digested material** (consolidated memory and
  recorded experience), never over a single raw observation, and only mature
  (`supported`/`validated`) principles reach the evaluator. A `candidate` has
  not survived a counterexample check yet, so it never steers a decision.
- **A correction is read again.** "Seen" is tracked per *revision*, so
  revising an already-assessed observation puts it back in front of the
  evaluator; repeating a conclusion still does not repeat the work, because
  the same objective from the same evidence stays one proposal.
- **A settled Ledger is cheap to re-check.** Each pass walks forward from the
  position it last reached instead of re-reading everything, so cost tracks
  what arrived, not what has accumulated. Those positions are caches, not
  queues: they reset on restart, and the durable records decide what has
  actually been seen.

Both LLM-backed steps need a reasoning backend. Without one the loop stays
quiet rather than writing unsynthesized summaries every tick.

### Knowledge Runtime quick start

Every piece is a plain Python object you can call directly — useful when
you're wiring the Knowledge Runtime into your own code rather than driving
it by hand. (If you just want to *run* something, skip ahead to the
**Command line** section below; it covers the same ground without writing
Python.) A full loop, end to end:

```python
import asyncio

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.knowledge import (
    Consolidator,
    GapRiskOpportunityDetector,
    GoalBridge,
    KnowledgeLedger,
    PrincipleExtractor,
    record_support,
    select_candidates,
)
from nexus_seed.knowledge.projection import (
    WorldStateProjection,
    annotate_world_fact,
    diff_world_views,
)
from nexus_seed.storage import Database, KnowledgeStore


async def main() -> None:
    # 0) One SQLite database, one KnowledgeStore, one KnowledgeLedger on top
    #    of it. The Ledger is the only object you write Knowledge through.
    db = Database("nexus_knowledge.db")
    ledger = KnowledgeLedger(KnowledgeStore(db))

    # 1) Record raw knowledge exactly as it came in — free text, no schema.
    #    `source` is provenance (where this came from), not a fact about the
    #    world yet.
    k1 = ledger.record(
        "B案の方がmarginはありそうだが、process追加が必要なのでschedule riskが高い。",
        source_type="meeting",
        source_ref="review_20260820",
    )

    # 2) Opt specific knowledge into the structured World View by attaching a
    #    "world_fact" annotation (entity/attribute/value). This does NOT
    #    rewrite k1's content — it appends a new revision that carries the
    #    extra reading alongside the original text.
    annotate_world_fact(ledger, k1.knowledge_id, entity="project-A", attribute="risk", value="schedule")

    # 3) Project the current World View (same {entity: {attribute: value}}
    #    shape as the existing StateStore), and remember it as a "before".
    view_before = WorldStateProjection(ledger).view()

    # ... time passes; something changes the picture ...
    k2 = ledger.record("A案のDRC riskが顕在化し、process側が追加工程を許容可能とした。", source_type="report")
    annotate_world_fact(ledger, k2.knowledge_id, entity="project-A", attribute="risk", value="none")

    # 4) Diff two World Views and hand each diff.to_events() result to the
    #    event loop with runtime.submit_event(event).
    view_after = WorldStateProjection(ledger).view()
    diff = diff_world_views(view_before, view_after)
    for change in diff.changes:
        print(change.entity, change.attribute, change.old_value, "->", change.new_value)

    # 5) Consolidate related raw knowledge into one memory. A real
    #    ExecutionBackend (e.g. LLMBackend) goes where FakeLLMBackend is here;
    #    without any backend at all, consolidate() still runs — it just lists
    #    the sources instead of synthesizing a summary, rather than guessing.
    candidates = select_candidates(ledger, kinds=("raw",))
    consolidate_backend = FakeLLMBackend(script=[proposal_response({
        "summary": "当初B案はschedule risk懸念だったが、A案のDRC riskが顕在化しB案再検討の合理性が高まっている。",
        "unresolved": [],
        "confidence": 0.7,
    })])
    memory = await Consolidator(ledger, consolidate_backend).consolidate(candidates)

    # 6) Extract a reusable principle from two or more related cases, then
    #    record independent confirmations until it matures past "candidate".
    principle_backend = FakeLLMBackend(script=[proposal_response({
        "principle": "早期の代替案再検討は、主要riskの顕在化に応じて柔軟に行うべきである。",
        "confidence": 0.6,
        "scope": None,
    })])
    principle = await PrincipleExtractor(ledger, principle_backend).extract(candidates)
    principle = record_support(ledger, principle, "evidence-1")
    principle = record_support(ledger, principle, "evidence-2")  # now "supported"

    # 7) Detect a Gap/Risk/Opportunity where a mature principle's condition
    #    matches the current World View.
    detect_backend = FakeLLMBackend(script=[proposal_response({
        "signals": [{
            "type": "opportunity",
            "description": "B案再検討の好機",
            "request": "project-AでB案の再検討を行う",
            "confidence": 0.8,
            "principle_id": principle.knowledge_id,
        }]
    })])
    signals = await GapRiskOpportunityDetector(detect_backend).detect(view_after, [principle])

    # 8) File each signal as a project proposal. That is where this stops:
    #    the autonomy policy decides whether it may proceed, a person decides
    #    anything that is not low-risk read-only work, and the loop is what
    #    submits an approved proposal to the Project Orchestrator.
    for signal, proposal in await GoalBridge(ledger).submit(signals):
        print(signal.type, proposal.status, proposal.knowledge_id)


asyncio.run(main())
```

A few things worth knowing before you reach for this:

- **Nothing is ever updated or deleted.** `ledger.revise(...)`,
  `ledger.annotate(...)`, and `ledger.relate(...)` each append a *new*
  revision; `ledger.history(k1.knowledge_id)` always returns every version
  ever written. Use `ledger.as_known_at(id, t)` to ask "what did we believe
  at time `t`" and `ledger.valid_at(id, t)` to ask "what was actually true at
  time `t`, as best we now know" — they can disagree, and that disagreement
  is the whole point of keeping both.
- **Nothing gets forced into a schema at write time.** `ledger.record(...)`
  only ever needs free-text `content` plus `source_type`/`source_ref`.
  Structure — a world-fact reading, a principle's scope, anything else —
  is always an optional `Annotation`/`metadata` a caller adds afterwards.
- **Every LLM-backed step degrades safely with no backend.**
  `Consolidator`, `PrincipleExtractor`, `CounterexampleSearcher`,
  `PredictionEngine`, and `GapRiskOpportunityDetector` all accept
  `backend=None` (the default) and either return nothing or a plainly
  unsynthesized answer — they never invent a conclusion, a counterexample, or
  a business risk to fill the gap. Pass a real `ExecutionBackend` (see
  `nexus_seed/backends/llm.py`) when you want the LLM-assisted behaviour.
- **There is exactly one route from Knowledge to a Project**: a proposal,
  judged by the autonomy policy, then submitted through
  `ProjectOrchestrator.submit()` by the loop. `GoalBridge` joins that route
  rather than running beside it — it files a proposal and stops, so a
  principle-driven finding gets the same gate, the same human review for
  anything that is not low-risk read-only work, and the same exactly-once
  submission as an evidence-driven one. A finding that cites no principle
  cites nothing, and is recorded `FORBIDDEN` rather than acted on.
- For a fuller tour, read `tests/test_knowledge_*.py` — each file is a
  runnable, self-contained example of one phase (K1 revisions/temporal
  queries, K2 projection/diff, K3 consolidation, K4 principles, K5 the Goal
  bridge, K6 experience/advisories).

### Command line (`nexus-seed-knowledge`)

Everything above is also reachable without writing Python, via one CLI with
a subcommand per operation:

```bash
nexus-seed-knowledge record --db k.db --content "B案の方がmarginはありそう" \
    --source-type meeting --about project-A
nexus-seed-knowledge fact --db k.db K-xxxx --entity project-A --attribute risk --value schedule
nexus-seed-knowledge view --db k.db
nexus-seed-knowledge diff --db k.db --before 2026-08-15T00:00:00 --emit-events
nexus-seed-knowledge consolidate --db k.db --about project-A
nexus-seed-knowledge extract-principle --db k.db --about project-A
nexus-seed-knowledge support --db k.db --principle-id K-xxxx --evidence-id ev-1
nexus-seed-knowledge predict --db k.db --principle-id K-xxxx --subject project-A
nexus-seed-knowledge evaluate --db k.db --prediction-id K-xxxx --actual '{"project-A": {"risk": "high"}}'
nexus-seed-knowledge signals --db k.db          # detect only
nexus-seed-knowledge propose --db k.db          # detect + file as proposals (the loop routes them)
nexus-seed-knowledge advise --db k.db --subject project-A
nexus-seed-knowledge --help                     # every subcommand, with its own --help
```

The autonomous loop is drivable from here too, so a headless deployment is
not limited to what the Cockpit can reach:

```bash
nexus-seed-knowledge watch-folder --db k.db /path/to/inbox --name 受信箱
nexus-seed-knowledge principles --db k.db       # what it learned, and how well each holds
nexus-seed-knowledge principles --db k.db --mature-only
nexus-seed-knowledge memories --db k.db         # what it compressed, and from what
nexus-seed-knowledge reconcile --db k.db        # run one bounded pass
nexus-seed-knowledge pending --db k.db          # everything waiting on a person
nexus-seed-knowledge decide --db k.db K-xxxx approve --note "..."
nexus-seed-knowledge answer --db k.db K-xxxx "自動更新はしません"
```

`decide` dispatches on what the id actually is — a proposal, an artifact, a
completion review or an unresolved entity — so you do not have to remember
which. Every one of these goes through the same `KnowledgeLoop` the Cockpit
calls, not a second implementation.

Run it as `python -m nexus_seed.knowledge_cli ...` if you haven't installed
the package's console scripts. One `--db` file is shared by the Knowledge
Ledger *and* the Project Orchestrator (one schema creates every table), so
`submit` can hand a detected Signal straight to a real Project on the same
database — no separate orchestrator setup needed unless you pass
`--orchestrator-db` to point it elsewhere.

Every LLM-backed subcommand (`consolidate`, `extract-principle`,
`counterexample`, `predict`, `signals`, `submit`) reads the same
`NEXUS_SEED_LLM_*` settings the rest of NEXUS SEED uses (see
`.env.example`) — set `NEXUS_SEED_LLM_ENABLED=true` there, or pass `--llm`
for one call; `--no-llm` always forces the conservative fallback described
above. `--json` on the read/write commands prints the full Knowledge
revision instead of a one-line summary, for scripting.

### Office files (.pptx / .xlsx / .docx) as observed situation

The file observer already watched, versioned and fingerprinted every file
under an authorized folder, whatever its type — what it could not do was
*read* an Office file, so a changed deck was noticed and its content never
arrived. Extractors for PowerPoint, Excel and Word close that:

```bash
pip install -e '.[ingest]'   # python-pptx, openpyxl, python-docx — not core deps
```

Drop a file into the watched folder and its content becomes Knowledge on the
next poll, through the ordinary Resource pipeline — no separate import step:

| File | Representation | Content |
| --- | --- | --- |
| `.pptx` | text | one block per slide, `[slide N]` marked |
| `.xlsx` / `.xlsm` | structure | `{sheet: {columns, rows, row_count}}`, formulas as their last cached value |
| `.docx` | text | paragraphs, then table rows |

Editing a watched file records a *second* observation rather than replacing
the first, so what the deck used to say stays on the record next to what it
says now. The readers are optional: without the extra installed, an Office
file is still versioned and its extraction fails with a message naming the
package to install, so installing it later picks the content up on the next
change.

For a one-off import that does not involve the watched folder,
`nexus-seed-knowledge pptx` records each slide as its own Knowledge object
(it reads slides through the same extractor, so both paths agree):

```bash
nexus-seed-knowledge pptx --db nexus_knowledge.db slide.pptx
nexus-seed-knowledge pptx --db nexus_knowledge.db --dir ./docs --about project-A
# equivalently: python -m nexus_seed.knowledge.ingest_pptx --db ... --dir ./docs
```

Each non-empty slide becomes one `kind=raw` Knowledge object
(`ledger.record(slide_text, source_type="pptx", source_ref="<file>#slide<N>")`),
related (`about`) to the deck — its filename by default, or the `--about`
value if given, so several decks about the same subject group together. That
relation is exactly what K3's `select_candidates(ledger, about=...)` filters
on, so the slides are immediately eligible for `Consolidator` /
`PrincipleExtractor` with no further wiring. Programmatic use:

```python
from nexus_seed.knowledge.ingest_pptx import ingest_pptx_file

created = ingest_pptx_file(ledger, Path("review_20260820.pptx"), about="project-A")
```

## How orchestration works

```text
incoming request
      |
      v
compile routing context (active Projects + World State + user context)
      |
      v
route: create Project / add task / update Project / ignore
      |
      v
assign or reuse exactly one Project Agent
      |
      v
delegate Goal or task over the audited A2A gateway
      |
      v
complete, update status, or wait on an escalation
```

An Agent reports back with a small management-level protocol:

| Message | Orchestrator effect |
| --- | --- |
| `PROJECT_STATUS` | Records the latest Project summary. |
| `PROJECT_COMPLETED` | Completes the Project and idles the Agent. |
| `NEED_CAPABILITY` | Blocks the Project with the missing capability. |
| `NEED_RESOURCE` | Blocks the Project with the missing resource. |
| `NEED_PERMISSION` | Blocks the Project with the missing permission. |
| `PROJECT_BLOCKED` | Records another blocking reason. |
| `NEED_HUMAN_INPUT` | Moves the Project to `WAITING_HUMAN`. |
| `DISCOVERED_NEW_PROJECT` | Routes the discovery as a new request. |

Only NEXUS SEED creates Projects. An Agent may report a discovery, but it
cannot create a Project directly.

## Install

Python 3.12 or newer is required.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## Project Orchestrator quick start

The example below uses the network-free Agent runtime. Without a routing
backend, the safe fallback is to create a new Project for each request.

```python
import asyncio

from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator


async def main() -> None:
    orchestrator = ProjectOrchestrator(
        "nexus.db",
        agent_runtime=InProcessAgentRuntime(),
    )
    try:
        decision = await orchestrator.handle_request(
            "Investigate the cause of the July sales decline"
        )
        project = orchestrator.projects.all()[0]
        print(decision.action.value, project.id, project.status.value)
    finally:
        orchestrator.close()


asyncio.run(main())
```

Pass an `ExecutionBackend` as `backend=` to enable semantic routing across the
current Project list. Pass an `AgentRuntime` as `agent_runtime=` to decide where
Project Agents run. `InProcessAgentRuntime` is a scripted fake;
`A2AAgentRuntime` delegates to a real external Agent, as the next section does.

## Running a Project on a real Agent

Start an external Agent Runtime in its own terminal. Any A2A agent works; the
example is Little Agent serving one of its profiles, with a workspace it may
read and write:

```powershell
$env:LITTLE_AGENT_WORKSPACE = "C:/work/project-agent"
little-agent --serve-a2a --agent analysis_worker --port 8801 --auto-approve
```

Point NEXUS SEED at it:

```dotenv
NEXUS_SEED_PROJECT_AGENT_RUNTIME=a2a
NEXUS_SEED_PROJECT_AGENT_URL=http://127.0.0.1:8801
# Optional: name an environment variable holding a bearer token.
NEXUS_SEED_PROJECT_AGENT_TOKEN_ENV=LITTLE_AGENT_A2A_TOKEN
# Optional: how long one Project may take before the Agent is called unreachable.
NEXUS_SEED_PROJECT_AGENT_TIMEOUT_SECONDS=1200
# Optional: a workspace the Agent can write, one directory per project.
NEXUS_SEED_PROJECT_WORKSPACE=projects
```

Then hand it a request:

```powershell
nexus-seed project "Analyze samples/sample_sales.csv and find why July 2026 sales fell, against June 2026"
```

```text
Routing: CREATE_PROJECT
Project: project-8a49c176-74c4-4bc5-9725-c10236e81205
Agent:   agent-f00bda11-396c-4823-a595-bd5d56a99666
Status:  COMPLETED
Goal:    Analyze sales data to identify reasons for low sales in July 2026 ...
Summary: ~92% of the revenue drop is concentrated in one store/category ...
```

NEXUS SEED sends the goal, context, constraints, and workspace — then stays
out of the way. The Agent decides the tasks, order, Skills, and tools; NEXUS
SEED does not inventory them. When it cannot continue
it escalates instead of retrying, and the Project is blocked with the reason:

```text
Status:  BLOCKED
Blocked: NEED_RESOURCE - there is no SAP historical file and no prior-year rows,
         so the comparison cannot be computed without fabrication
```

An Agent Runtime that is down is a different thing from a Project that cannot
proceed: the Project keeps its state, the failure is recorded on the Agent, and
running the command again delegates it once more.

Leave `NEXUS_SEED_PROJECT_AGENT_RUNTIME` unset (or `in_process`) to use the
deterministic runtime — the orchestration is identical either way.

`nexus-seed project` waits for the Project by default because it is the
explicit door; pass `--no-wait` for the same hand-off the resident server does.

It works directly on the database, so use it when no NEXUS SEED is resident on
that data directory. When one is running, send requests with `nexus-seed task`
instead: one process reconciling a Project is the assumption, and two would ask
the same Agent for the same answer.

## The context you write yourself

NEXUS SEED can watch files and machines, but it cannot guess what you *meant*
by "done", or which of two words you use for the same thing. Three files say
so, in your own prose:

```text
NEXUS_SEED_DATA_DIR/context/
  terms.md       what the words mean here
  goals.md       what a good state looks like, and what constrains it
  situation.md   what is true right now
```

They are created empty on first start, and read exactly as written. Nothing
parses them into a schema, and a goal is never converted into a metric — "BLOCKED
Projectを放置しない" stays that sentence, and what comes back is a sentence about
what is missing, not a number you did not ask for. Editing a file appends a
revision, so what you used to believe stays on the record.

Read the notes against the live world:

```powershell
nexus-seed-knowledge assess --db nexus_seed.db
```

```text
goal_gaps:
    BLOCKED Projectが存在する
        evidence: goals.md
        evidence: situation.md

(1 task candidate(s) waiting for a decision)
```

Five things are looked for, and finding nothing is a normal answer:
`terminology_issues` (a word used two ways, or never defined),
`contradictions` (two things that cannot both be true), `goal_gaps` (a goal the
situation does not meet), `unknowns` (something that has to be settled first),
and `task_candidates` — what someone could do about the rest.

**A candidate is a suggestion, and v0.1 never acts on one.** It waits:

```powershell
nexus-seed-knowledge candidates --db nexus_seed.db
```

```text
[PENDING_REVIEW] K-task-candidate-190a67ac32923a3cde6f  (AGENT, confidence=0.85)
    Project Aのblockerを調査する
    reason: goals.mdはBLOCKEDを放置しないと述べている
```

Three answers, in the CLI and as three buttons in the Cockpit's **Task候補**
section:

```powershell
# 実行 — goes through the ordinary door, exactly like a message you typed
nexus-seed-knowledge candidate --db nexus_seed.db <id> approve
# 無視 — kept with the reason, never deleted
nexus-seed-knowledge candidate --db nexus_seed.db <id> reject --note "いまはやらない"
# 修正 — your wording replaces the machine's, and it stays waiting
nexus-seed-knowledge candidate --db nexus_seed.db <id> amend \
    --description "Project Aの担当者に直接確認する" --note "調査より先に人に聞く"
```

Approving calls the same `ProjectOrchestrator.submit()` a human message uses,
so the router decides whether this is a new Project or another Task on one that
already exists. Nothing here creates a Project itself.

An amendment is the interesting one. It is the clearest record there is of how
this system's suggestions differ from what you actually wanted, so it is kept
as a revision — the original wording is still in the object's history, and a
later Principle Extraction is entitled to learn from the difference.

Everything on this path is Knowledge: the documents, each reading, each
candidate, and every approval, refusal and edit. Without a reasoning backend
the assessor finds nothing and records nothing, because "no contradiction" and
"nobody looked" are different answers and only the second one would be true.

Re-reading is skipped when nothing moved. The check is made before the model is
called, and it covers both the files *and* each live Project's status — so a
Project going BLOCKED is a new situation worth reading even though nobody typed
anything.

## Giving a Project a resource it asked for

A Project Agent gets its own workspace directory and nothing else. That is
deliberate: NEXUS SEED never hands an Agent the host it happens to be running
on. So when the Agent needs a file it was not given, it does not go looking —
it escalates with `NEED_RESOURCE` and the Project blocks, exactly as above.

What it may reach is decided per Project, and the Project is the ceiling:

```text
NEXUS SEED Project              little_agent
  workspace              ->       workspace
  readable_resources     ->       readable_paths
  writable_resources     ->       writable_paths
```

Answering that is one command. First say which trees a Project may ever be
given anything from — with neither set, nothing outside a workspace is
grantable at all:

```dotenv
# Readable roots (os.pathsep-separated: ";" on Windows, ":" elsewhere).
NEXUS_SEED_PROJECT_RESOURCE_READ_ROOTS=C:/work/shared;C:/work/specs
# Of those, the ones a Project may also write back into. Readable does not
# imply writable, which is why this is a separate list.
NEXUS_SEED_PROJECT_RESOURCE_WRITE_ROOTS=C:/work/shared/out
```

Then look at what is being asked for, and answer it:

```powershell
nexus-seed-knowledge grants --db nexus_seed.db
```

```text
[BLOCKED] project-8a49c176-...  Analyze sales data to identify reasons ...
    REQUESTED read       (no uri - a person has to say which resource)
              asked for: SAP historical export for FY2025
              reason:    the comparison cannot be computed without it
```

```powershell
nexus-seed-knowledge grant --db nexus_seed.db project-8a49c176-... `
    "file:C:/work/shared/sap_fy2025.csv" --reason "前年比較の元データ"
```

```text
granted: read access to file:C:/work/shared/sap_fy2025.csv by reference is within policy
```

The same Task then continues. Nothing is restarted and no new Project is
created: the *same* Project is delegated to the *same* Agent with the resource
available to it, because nothing about the task changed except what it can
reach.

### Read is a link, write is a copy

There is no third option and nothing to choose — the access decides:

| grant | what the Agent gets | when it reaches the real file |
| --- | --- | --- |
| `--access read` *(default)* | the original, where it lives | never |
| `--access read_write` | its own copy, in the workspace | when you accept the work |

**Reading does not need a duplicate.** The Agent is pointed at the file, so it
sees the current contents and nothing is copied. A directory works the same
way, which is how you hand over a folder of material:

```powershell
nexus-seed-knowledge grant --db nexus_seed.db <project-id> "file:C:/work/shared/docs"
```

**Writing does need one.** The Agent edits its own copy under `resources/`, so
the real file is untouched while the work is in progress — a task that is
abandoned, refused, or simply goes wrong leaves nothing behind in it. One
command is the moment the edits arrive:

```powershell
nexus-seed-knowledge collect --db nexus_seed.db <project-id>
```

The cost is that a **writable directory cannot be granted**: copying a tree of
unknown size into a workspace is not something to do quietly. Grant the
directory for reading, and name the file inside it that the task has to change.

```text
refused: 'C:/work/shared/docs' is a directory; grant it read, or name the file
         inside it that the task has to change
```

A read grant is a link to the original, and nothing here pretends otherwise:
an Agent that ignores its instructions can open a read-granted path for
writing. What a read grant guarantees is what it says — the file is reachable,
and no edit of it is ever collected back into anything. If the original has to
be safe from the Agent, grant it for writing so it is copied, and decide at
`collect` whether the edits are kept.

### What reaches the Agent

Whatever the mix, a delegation carries:

```text
workspace        the task's own directory
readable_paths   the originals it may read, where they live
writable_paths   its own copies, the files it may change
resources        every grant, with its path, access and delivery
```

A Bridge starting `little_agent` passes those three straight through as its
`workspace` / `readable_paths` / `writable_paths`. Over A2A they ride in
`metadata` under one extension URI, so an agent that does not know the
extension sees an ordinary, valid message. The workspace also holds a
`RESOURCES.md` and a `.nexus-seed/manifest.json` saying the same thing, for an
agent that reads files rather than metadata.

A Task may reach **less** than its Project allows — put the uris it needs in
`task["context"]["resources"]` and the delegation narrows to those. It can
never reach more: naming something the Project was not granted adds nothing.

Everything above is refused unless it is inside an authorized root, and the
check resolves symlinks first, so a link pointing out of the tree is refused
rather than followed:

```text
refused: target 'C:/work/private/keys.txt' escapes allowed_root C:/work/shared
```

The decision is deterministic policy code, never a model: an LLM that can be
talked into widening its own access is not a boundary. The Cockpit shows the
same request under **Needs Attention** and grants it from the Project view, and
`resource:<uri>` (a Resource version) and `knowledge:<id>` (a Knowledge object)
can be granted the same way — Knowledge read-only, since it is an append-only
record.

## Normal operation

Ordinary requests always become Projects. Run NEXUS SEED and submit work:

```powershell
nexus-seed                                        # resident
nexus-seed task "Analyze samples/sample_sales.csv and find why July fell"
```

`task` returns as soon as the request is accepted — a Project can take as long
as it takes. What happens next happens on the server's own tick:

```text
CLI / webhook / connector -> Ingress -> human_message -> ProjectRouter
                                                      -> Project + Agent
                                                      -> A2A hand-over
```

The hand-over is durable. What the Agent owes NEXUS SEED — which goal or task,
the remote handle, how many attempts, when to retry — is written to SQLite, so
stopping NEXUS SEED mid-run loses nothing: on the next start it re-adopts the
same Agent and picks up whatever happened while it was down. An Agent Runtime
that is unreachable is retried a few times and then left alone; it never turns
into a blocked Project.

Watch it in the Cockpit's **Projects** view (`/cockpit`), which reads the
orchestrator's own records. There is one kind of project now, so it is shown
once.

`nexus-seed task` also still updates World State through the existing
interpretation path: the flag adds the Project route, it does not remove
perception.

### Unblocking a Project

When an Agent cannot continue it escalates, and the Project is blocked with the
reason. Answer it the same way you asked for it:

```powershell
nexus-seed task "Analyze sales.csv and compare it against SAP prior-year data"
# -> NEED_RESOURCE: there is no SAP data here -> BLOCKED

nexus-seed task "Forget SAP. Carry on with the June data we already have."
# -> ADD_TASK_TO_PROJECT on the same project, same agent -> ACTIVE -> COMPLETED
```

The router is given the blocked and waiting Projects along with the active
ones, so an answer reaches the Project that is waiting for it instead of
starting a second one. The blocker is not deleted when it clears — it is marked
resolved, and by what, so the Project's history still says what once stopped it.

This is the one decision NEXUS SEED makes for itself, so it depends on the
routing model answering inside `NEXUS_SEED_LLM_TIMEOUT_SECONDS`. When it does
not, the router falls back: a request that came from inside a project becomes a
Task on that project, because the person was looking at it when they wrote it;
a request with no origin becomes its own project, because burying it inside an
unrelated one is worse than an extra one. Either way the reason says
`fallback`. If requests keep falling back, raise that timeout (a large local
model can need several minutes) or route with a faster model.

## Configuration

Two language models are involved in a running system, and their settings look
alike. They are not the same thing:

| | What it is | Where it is configured |
| --- | --- | --- |
| **NEXUS SEED's own reasoning** | The model NEXUS SEED *thinks* with: routing a message to a Project, answering a question about one, interpreting an Event, choosing a plan | `NEXUS_SEED_LLM_*` in this `.env` |
| **The model that does the work** | The Project Agent's own model | **Not here.** A Project is delegated whole; the Agent brings its own model, keys, skills and config file. `NEXUS_SEED_PROJECT_AGENT_*` says *where* the Agent is, never what it thinks with or what it can do |

`in_process`, the default Agent Runtime, is local and network-free. It reuses
the configured NEXUS SEED reasoning backend and has no direct tool authority.
Set `NEXUS_SEED_PROJECT_AGENT_RUNTIME=a2a` to delegate a whole Project to an
external Agent Runtime with its own model, keys, skills, and configuration.

A third thing is configured here and is neither: **what a Project may be
given.** `NEXUS_SEED_PROJECT_RESOURCE_READ_ROOTS` and
`..._WRITE_ROOTS` name the host directories a task can ever be granted a file
from. They are empty by default, and an empty list means nothing outside a
task's own workspace is grantable — see [Giving a Project a resource it asked
for](#giving-a-project-a-resource-it-asked-for).

Rather than reading `.env` to work out which is which, ask:

```powershell
nexus-seed config
nexus-seed config --json
```

It prints the resolved reasoning and delegation settings and says what each is
used for. It reads only — nothing is started and nothing is connected to. An API key is
named by the variable that holds it, so the report says which variable is
consulted and whether it has a value, never the value.

`.env.example` is laid out in the same groups.

## Application

Configure a data directory outside the source tree and a webhook token:

```dotenv
NEXUS_SEED_DATA_DIR=C:/Users/user/AppData/Local/nexus-seed
NEXUS_SEED_WEBHOOK_HOST=127.0.0.1
NEXUS_SEED_WEBHOOK_PORT=8787
NEXUS_SEED_WEBHOOK_TOKEN=replace-this-token
NEXUS_SEED_COCKPIT_ENABLED=true
```

Then run:

```powershell
nexus-seed --once
nexus-seed
```

The Cockpit is served at `http://127.0.0.1:8787/cockpit`.
Useful commands include:

```powershell
nexus-seed status
nexus-seed task "Analyze this request"
nexus-seed project "Run this request directly"
nexus-seed config
```

Each project has one thread and one box:

```text
POST /projects/<id>/message  {"message": "今どうなってる？"}
POST /projects/<id>/message  {"message": "地域別の内訳も出して"}
```

Both go to the same place, because deciding which one a message is, is NEXUS
SEED's job rather than the person's. The `ProjectRouter` reads it for meaning;
its `IGNORE` action already means "this needs no project work at all", which is
what a question is. So `IGNORE` is answered read-only from the project's
situation, and anything else is carried out and handed to the Agent. Both
outcomes are appended to the same durable thread, so a project's history reads
as one conversation.

`POST /projects/<id>/chat` remains the read-only half on its own, for a caller
that only ever asks.

Either judge can be wrong, so `{"act": true}` lets the person overrule it: this
message is an instruction, hand it over. The Cockpit offers it as **指示として
渡す** on a reply that explained. That is the whole correction — one bit, not a
second box and not a verb to learn.

With no reasoning backend nothing can judge meaning, so the deterministic
change-request guard decides instead, and everything it does not recognise is
explained rather than delegated. An answer can be ignored; delegated work
cannot be un-delegated.

### Human decisions

`/control`, the slash-command parser, and the generic Continuation review API
are not application surfaces. Project instructions go to the Project
Orchestrator; proposal, Artifact, completion, question, and Entity decisions
go through the Knowledge API shown by the Cockpit's World view.

### Goal is gone; a Project is the goal

There is no Goal record, no `ControlStore`, no goal decomposition. A Project
*is* the objective a person handed over, delegated whole to one Agent, and the
Agent breaks it into tasks itself — so there was nothing left for a Goal to be.

What that removed: `nexus_seed/control/`, `storage/control_store.py`,
`processes/control.py` (`evaluate_goal` and the LLM decomposition),
`nexus_seed/orchestration/` (the Goal loop), the Goal-derived Project
projection, `ProcessResult.goals` / `goal_updates`, `ctx.record_goal` /
`update_goal`, `ctx.services.get_goal`, and the `goals`, `commands`,
`command_results` and `human_identities` tables. `review_human_work` survives
as `processes/work_review.py`: approving constrained Work was never a Goal
concern.

### Instructing an orchestrator Project

`POST /projects/<id>/message` above is how a person does this. The direct
endpoints are still there for a caller that has already decided:

```text
POST /cockpit/api/orchestrator/projects/<id>/instruct  {"message": "地域別の内訳も出して"}
POST /cockpit/api/orchestrator/projects/<id>/unblock   {"note": "SAP権限を付与した"}
```

There is no command vocabulary here and no need for one. The instruction goes
straight to the `ProjectRouter` with `origin_project_id` set, so it decides —
semantically — whether this is more work for the Project (`ADD_TASK_TO_PROJECT`,
same Agent) or an independent Goal (`CREATE_PROJECT`, a child Project with its
own Agent). NEXUS SEED still only creates or extends a Project and delegates it;
it never executes the work.

Pass a `request_id` to make the delivery exactly-once:

```text
POST .../instruct  {"message": "...", "request_id": "b0f1…"}
```

A resend with the same id replays the recorded decision instead of routing,
spawning or delegating again, so a double click or a retried HTTP call cannot
hand the Agent the same task twice. The ledger is durable, so a replay is still
a replay after a restart. Without a `request_id` the call is handled as a fresh
request, exactly as before. The Cockpit mints one key per instruction and keeps
it until the request succeeds.

`unblock` resolves the Project's blockers and hands it back to its Agent.
Blockers are not deleted: each is stamped `resolved_at` / `resolved_by`, so why
the Project stopped stays readable afterwards.

## Design boundaries

The six fixed primitives remain `Event`, `Process`, `State`, `Context`,
`Continuation`, and `Runtime`. Project, Agent, Skill, Work, Capability, and
Knowledge are domain records or Process roles, not new primitives.

The redesign keeps these responsibilities separate:

```text
Project     what NEXUS SEED decides exists and steers
Agent       who owns execution for one Project
Skill       a reusable cognitive procedure
Capability  what can be done
Work        what needs to be done inside a Project
Provider    where execution happens
```

Durability belongs to NEXUS SEED; execution belongs to the Project Agent. The
Agent returns only management-level status and escalation messages over A2A.

## Repository layout

```text
nexus_seed/app/                      composition root and application flows
nexus_seed/modules/observer/         sources, ingress, and observation adapters
nexus_seed/modules/knowledge/        Knowledge ledger, search, and projections
nexus_seed/modules/planner/          Knowledge/Observation assessment and proposals
nexus_seed/modules/project_manager/  lifecycle, assignment, workspace, and A2A domain
nexus_seed/integrations/             webhook, LLM, and external A2A transports
nexus_seed/policy/                   approval policy
nexus_seed/platform/                 stable contract and mechanism facades
nexus_seed/core/                     six fixed primitives (stable implementation path)
nexus_seed/runtime/                  durable Runtime (stable implementation path)
modules/little_agent/                independent Git submodule; connected only over A2A
tests/                               unit, acceptance, and restart-convergence tests
```

The former `knowledge/`, `orchestrator/`, `adapters/`, `providers/`,
`workspace/`, and module-owned `storage/` paths are compatibility facades. New
code should import the owning module path. See
[Module boundaries](docs/module-layout.ja.md) for the migration map and the
directories deliberately retained at platform level.

## Development

```powershell
pytest
python -m nexus_seed.app --once
```

`pytest` never needs an external Agent: the orchestrator tests use
`InProcessAgentRuntime`, and the A2A boundary is tested against a scripted local
HTTP server — including the durable hand-over, the bounded retry, and restart
reconciliation. The end-to-end tests that need a real Agent live in
`tests/integration/` and skip themselves unless one is pointed at:

```powershell
$env:NEXUS_SEED_PROJECT_AGENT_URL = "http://127.0.0.1:8801"
pytest tests/integration
```

## Documentation

- [Architecture](docs/architecture.md)
- [アーキテクチャ詳細（日本語）](docs/architecture.ja.md)
- [モジュール境界と互換パス](docs/module-layout.ja.md)
- [Contributor invariants and working agreement](AGENTS.md)
