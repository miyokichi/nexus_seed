# NEXUS SEED Architecture

*[日本語](architecture.ja.md) · [README](../README.md)*

NEXUS SEED is a single Python application centered on a durable Project
Orchestrator and Knowledge Runtime. It turns explicitly permitted information
into an updated situation, decides which Project should exist next, and
delegates each Project to a Project Agent.

```text
External information / Agent reports / human input
                         ↓
                 Knowledge Runtime
                         ↓
                Situation Evaluation
                         ↓
                Project Orchestrator
                         ↓
               1 Project = 1 Agent
                         ↓
             results / questions / observations
                         └────────→ Knowledge Runtime
```

## Responsibility boundaries

| Layer | Responsibility |
| --- | --- |
| Knowledge Runtime | Append evidence with provenance, project a World View, detect gaps, risks, and opportunities |
| Project Orchestrator | Create, update, prioritize, assign, and lifecycle Projects |
| Agent Runtime | Decompose one Project, choose tools, and perform the work |
| Durable Runtime | Persist Event delivery, Process execution, suspension, timers, and spawn/join |
| Cockpit | Present Projects, World state, questions, artifacts, and pending reviews to a human |

The Runtime contains no domain or LLM judgement. Judgement belongs to
Knowledge or Process handlers; execution belongs to the Agent Runtime.

## The fixed six primitives

The core contains exactly six primitives:

- `Event`: an append-only record that something happened
- `Process`: a static `ProcessDefinition` and running `ProcessInstance`
- `State`: current facts with version history
- `Context`: a read-only activation view regenerated from Memory
- `Continuation`: a logical, Event-matched resume condition
- `Runtime`: the mechanism coordinating delivery, execution, and persistence

Project, Agent, Knowledge, and Resource are domain records, not new core
primitives.

## Project Orchestrator

Every ordinary request enters `ProjectOrchestrator.submit()`. The Router chooses
one of four outcomes: create a Project, add a Task to an existing Project,
update an existing Project, or ignore an already-covered request.

A Project has at most one assigned Agent at a time. Projects may form a parent
and child hierarchy, but Agents do not communicate directly with each other;
coordination always goes through the Orchestrator.

An Agent completion report does not complete the Project immediately:

```text
Agent COMPLETED
      ↓
Project WAITING_REVIEW
      ↓
Artifact PENDING_REVIEW
      ├─ approve → APPROVED → Project COMPLETED
      └─ reject  → REJECTED → correction Task to the same Project and Agent
```

Approve and reject actions enter through the Cockpit and append Knowledge
revisions. Request keys suppress double clicks, and restart reconciliation
returns corrections to the same Project and Agent.

## Knowledge Runtime

The Knowledge Ledger is append-only. Corrections, annotations, relations,
consolidated memories, principles, predictions, and review decisions create new
revisions instead of overwriting history.

Knowledge and the World View are separate:

```text
Raw Knowledge
  ├─ annotation / relation
  ├─ consolidated memory
  ├─ principle / prediction
  └─ world_fact annotation ──→ World Projection
                                      ↓
                            gap / risk / opportunity
                                      ↓
                            Orchestrator.submit()
```

The World Projection is rebuildable from the Ledger. A detector never creates
a Project directly; it uses the same public Orchestrator entry point as a human
request. The autonomous loop may act automatically only inside its configured
low-risk, high-confidence, read-only envelope. Everything else waits for human
confirmation in the Cockpit.

## Agent Runtime

Two transports are currently available:

- `InProcessAgentRuntime`: deterministic/local integration plus a bounded LLM
  Agent that analyzes only supplied Evidence and performs no direct PC or tool
  side effects
- `A2AAgentRuntime`: delegates a complete Project to an external Agent and
  journals A2A messages

Both sit behind the same Orchestrator interface, so future transports do not
change the Project or Knowledge models.

## Durable Runtime

The supporting event-driven runtime provides this loop:

```text
persist Event
  → create EventDelivery in the same transaction
  → Router starts or resumes a Process
  → Executor atomically commits ProcessResult
  → persist emitted Events
```

Event persistence and delivery are separate states. Undelivered Events recover
after restart. `trigger_event_id` prevents a redelivery from starting the same
logical Process twice.

Suspension stores a Continuation's `resume_point`, `waiting_for`, and
`saved_process_state`, never a Python call stack. On resume, Context is
recompiled from current Memory.

Opening an older database is non-destructive. Tables from retired features are
left as historical data, while Process definitions whose handlers are no
longer registered cannot start or resume. A new database creates only the
active schema.

## Ingress and Resources

External input becomes an Event only through Ingress:

- the adapter supplies `source_event_key`;
- a database constraint on `(adapter_id, source_event_key)` deduplicates
  redelivery;
- receipt, Event, and checkpoint persist atomically;
- an adapter reports occurrences but never interprets their meaning.

Persistent external objects have three distinct levels:

```text
Resource            identity and URI
ResourceVersion     immutable bytes at one point in time
Representation      text / structure / metadata extracted from that Version
```

Extraction is an ordinary Process, not a Runtime feature. Context Snapshots
record the exact ResourceVersion and Representation read by an activation.

## Active SQLite data

New databases create only these areas:

- Events and delivery: `events`, `event_deliveries`
- Processes: `process_definitions`, `process_instances`, `process_activations`
- Resume mechanics: `continuations`, `timers`, `joins`
- Context and State: `context_snapshots`, `world_state_history`,
  `world_state_current`
- Ingress and Resources: `ingress_receipts`, `adapter_checkpoints`, `resources`,
  `resource_versions`, `resource_representations`
- Projects: `orchestrator_projects`, `orchestrator_agents`,
  `orchestrator_a2a_messages`, `orchestrator_instructions`
- UI and Knowledge: `project_chat_threads`, `project_chat_messages`,
  `knowledge_revisions`

## Source layout

```text
nexus_seed/
├── core/           # pure models for the six primitives
├── runtime/        # router, scheduler, executor, dispatcher
├── storage/        # sqlite3 stores
├── context/        # per-activation Context compiler
├── delivery/       # durable EventDelivery
├── ingress/        # external observation boundary
├── adapters/       # manual, webhook, local file
├── resources/      # Resource / Version / Representation
├── processes/      # active ordinary Process handlers
├── knowledge/      # append-only ledger and World Projection
├── orchestrator/   # Project / Agent / A2A lifecycle
├── providers/      # A2A transport client
├── chat/           # project-scoped conversation
└── cockpit/        # human review UI and API
```

## Verification

```bash
pytest
python -m nexus_seed.app --once
```

The suite uses temporary SQLite databases and covers restart, redelivery,
duplicate review actions, Resource provenance, and Project/Agent reconciliation.
