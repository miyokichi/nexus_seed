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
`Continuation`, and `Runtime`. Project, Agent, Skill, Work, and Capability are
domain records or Process roles, not new primitives.

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
