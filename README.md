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
- ordinary requests — `nexus-seed task`, a webhook, any connector — routed into
  Projects behind `NEXUS_SEED_PROJECT_ORCHESTRATOR_ENABLED`; and
- a Cockpit **Projects** view over the orchestrator's own records.

What is deliberately not built yet: several Agents on one Project, and Agents
talking to each other.

The earlier durable runtime remains in the repository, is tested, and provides
the compatibility application. See
[the redesign inventory](docs/orchestrator-redesign-inventory.md) for the exact
`KEEP`, `MOVE_TO_AGENT_RUNTIME`, and `DEPRECATE` classification. No earlier
module was deleted by the redesign.

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
- a Goal bridge that turns a detected Gap/Risk/Opportunity into an ordinary
  request through the existing `ProjectOrchestrator.submit()` — it never
  creates a Project directly.

It is available as a library today, with its own test suite
(`tests/test_knowledge_*.py`); `app.py`, the demo, and Cockpit do not wire it
in yet — you use it by importing `nexus_seed.knowledge` directly, as below.
See [Architecture and phase history](docs/architecture.md) for the full
design and [AGENTS.md](AGENTS.md) for the invariants it keeps.

### Knowledge Runtime quick start

There is no CLI or server for this yet — every piece is a plain Python object
you call directly. A full loop, end to end:

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
from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator
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

    # 4) Diff two World Views and hand the result to the existing event loop
    #    (runtime.submit_event(event) for each event in diff.to_events()) —
    #    same "state_changed" shape apply_state_delta already emits.
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
    #    matches the current World View, then hand it to the *existing*
    #    ProjectOrchestrator through its own public submit() — the bridge
    #    never creates a Project itself.
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

    routing_backend = FakeLLMBackend(script=[proposal_response({
        "action": "CREATE_PROJECT",
        "proposed_goal": "project-AでB案の再検討を行う",
        "reason": "opportunity",
        "confidence": 0.9,
    })])
    orchestrator = ProjectOrchestrator(
        "nexus.db", agent_runtime=InProcessAgentRuntime(), backend=routing_backend
    )
    for signal, decision, project in await GoalBridge(ledger, orchestrator).submit(signals):
        print(decision.action.value, project.goal if project else None)
    orchestrator.close()


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
- **`GoalBridge` only ever calls `orchestrator.submit()`/`.handle_request()`**
  — the same public entry point a human message uses. It cannot create,
  modify or route a Project on its own; the Project Orchestrator's own
  `ProjectRouter` still decides what happens to the request.
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
nexus-seed-knowledge submit --db k.db           # detect + hand to the real ProjectOrchestrator
nexus-seed-knowledge advise --db k.db --subject project-A
nexus-seed-knowledge --help                     # every subcommand, with its own --help
```

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

### Ingesting local files (.pptx) into Knowledge

`nexus_seed/knowledge/ingest_pptx.py` (also `nexus-seed-knowledge pptx`) is a
small, standalone first step for pulling local documents in — it is *not*
the eventual Resource/ingress pipeline integration (see
`nexus_seed/resources/extractors.py`, whose extractor registry is built for
exactly this but does not have an Office/PDF entry yet); it is a script you
can run today.

```bash
pip install -e '.[ingest]'   # adds python-pptx; not a core dependency

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

NEXUS SEED sends the goal, its context, its constraints, a workspace and the
contracts of the Skills in `skills/` — then stays out of the way. The Agent
decides the tasks, the order, and which Skills apply. When it cannot continue
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

## Normal operation

With the orchestrator switched on, ordinary requests become Projects. Run
NEXUS SEED, and talk to it the way you already did:

```dotenv
NEXUS_SEED_PROJECT_ORCHESTRATOR_ENABLED=true
```

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

`in_process`, the default Agent Runtime, calls no model at all — it is
deterministic and network-free. Set `NEXUS_SEED_PROJECT_AGENT_RUNTIME=a2a` to
delegate for real.

Skills split the same way, and for the same reason. `NEXUS_SEED_SKILL_ROOTS` is
what **NEXUS SEED itself** can do — those Skills are imported as Processes and
become its capabilities:

```dotenv
# Highest precedence first; ";" on Windows, ":" elsewhere.
# Unset means ./skills then ~/.nexus_seed/skills.
NEXUS_SEED_SKILL_ROOTS=./skills:/team/shared-skills
NEXUS_SEED_SKILLS_STRICT=false
NEXUS_SEED_SKILLS_ON_DUPLICATE=override
```

A **Project Agent's** skills are not these and are not configurable here. A
Project is delegated as a *goal*, not as a method: the assignment carries the
goal, its context, its constraints and a workspace, and nothing about how to
meet it. The Agent reads its own skills from its own configuration file, and
NEXUS SEED neither sends them nor knows what they are.

Rather than reading `.env` to work out which is which, ask:

```powershell
nexus-seed config
nexus-seed config --json
```

It prints the resolved settings in those same groups, says what each one is
used for, and lists the Skills it actually found and the roots it looked in. It
reads only — nothing is started and nothing is connected to. An API key is
named by the variable that holds it, so the report says which variable is
consulted and whether it has a value, never the value.

`.env.example` is laid out in the same five groups.

## Compatibility application

The existing event-processing application is still available while the new
orchestrator is integrated. Configure a data directory outside the source tree
and a webhook token:

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

The compatibility Cockpit is served at `http://127.0.0.1:8787/cockpit`.
Useful commands include:

```powershell
nexus-seed status
nexus-seed task "Analyze this request"
nexus-seed reviews
nexus-seed review <review-id> approve
```

These commands exercise the durable Work/Capability runtime alongside the
`ProjectOrchestrator` API.

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

### The command surface is gone

`/control`, the slash-command parser, `ConsoleService` and the Control Plane
instruction box no longer exist. What they gated is reachable directly:

| What a person does | Where it goes now |
| --- | --- |
| Start or extend work | `POST /cockpit/api/orchestrator/projects/<id>/instruct`, or a `human_message` — both reach the `ProjectRouter` |
| Approve or reject a review | `POST /cockpit/api/reviews/<id>/<approve\|reject>` (`nexus_seed/reviews.py`) |
| Answer a self question | `POST /cockpit/api/questions/<id>/answer` (`nexus_seed/questions.py`) |
| Unblock a Project | `POST /cockpit/api/orchestrator/projects/<id>/unblock` |

A review is a Continuation waiting for an event whose type ends in
`_reviewed`, so deciding one is emitting that event; a self question is
answered the same way. Both are idempotent by construction — the second call
finds nothing waiting and changes nothing — and neither authorizes anything,
because the gate is the channel's own (the webhook bearer token). The shipped
app only ever granted one identity `command.*`, so nothing was being denied.

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

What Phase 6 reads instead is a **pursuit** (`nexus_seed/pursuit.py`) — an id,
an objective, whether it is live, and what should make it reconsider.
`Runtime.register_pursuit_source(name, source)` registers an answer,
`runtime.active_pursuits()` lists them, `runtime.get_pursuit(id)` resolves one
whether or not it is still live (an Intention has to keep describing something
that just finished). `bootstrap_project_orchestration` registers
`ProjectPursuits`, so Phase 6 holds its Intentions about Projects. A runtime
with no source pursues nothing: `project_self` reports no active ids and the
startup wake stays silent.

`IntentionRecord.pursuit_id` is text (a Project id is `project-<uuid>`), and
both it and event payloads still carry `goal_id` so a journal written before
the rename stays loadable.


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
nexus_seed/knowledge/     Knowledge Ledger, World Projection, Consolidation, Principles
nexus_seed/orchestrator/  Project routing, lifecycle, Agent assignment, A2A
nexus_seed/storage/       SQLite stores, including orchestrator records
nexus_seed/core/          the six fixed data models
nexus_seed/runtime/       earlier durable event runtime
nexus_seed/processes/     Process handlers, including the human review gate
nexus_seed/providers/     provider federation, A2A client, Project Agent transport
nexus_seed/cockpit/       compatibility read model and dependency-free Web UI
skills/                   directory Skills (skill.json + SKILL.md)
tests/                    unit, acceptance, and restart-convergence tests
```

## Development

```powershell
pytest
python -m nexus_seed.demo
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

- [Project Orchestrator redesign inventory](docs/orchestrator-redesign-inventory.md)
- [Architecture and phase history](docs/architecture.md)
- [Architecture inventory](docs/architecture-inventory.md)
- [アーキテクチャ詳細（日本語）](docs/architecture.ja.md)
- [機能棚卸し（日本語）](docs/architecture-inventory.ja.md)
- [Contributor invariants and working agreement](AGENTS.md)
