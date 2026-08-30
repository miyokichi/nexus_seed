# NEXUS SEED アーキテクチャ

*[English](architecture.md) · [README](../README.ja.md)*

NEXUS SEEDは、永続化されたProject OrchestratorとKnowledge Runtimeを中心に、
ユーザーが許可した情報から状況を更新し、次のProjectを判断し、Project Agentへ委譲する
単一Pythonアプリケーションです。

```text
外部情報 / Agent報告 / 人間の入力
                ↓
        Knowledge Runtime
                ↓
       Situation Evaluation
                ↓
       Project Orchestrator
                ↓
      1 Project = 1 Agent
                ↓
      成果・質問・新しい観測
                └────────→ Knowledge Runtime
```

## 責務の境界

| 層 | 責務 |
| --- | --- |
| Knowledge Runtime | 根拠付き情報を追記し、World Viewを投影し、Gap・Risk・Opportunityを検出する |
| Project Orchestrator | Projectを作成・更新・優先付けし、担当Agentとライフサイクルを管理する |
| Agent Runtime | 1つのProjectのTask分解、Tool選択、実作業を担当する |
| Durable Runtime | Event配信、Process実行、中断・再開、timer、spawn/joinを永続化する |
| Cockpit | Project、World、質問、成果物、承認待ちを人間へ提示する |

Runtimeに業務判断やLLM判断は置きません。判断はKnowledgeまたはProcess handler、実作業は
Agent Runtimeに属します。

## 固定された6つのプリミティブ

中核プリミティブは次の6つだけです。

- `Event`: 起きたことを表す追記専用の事実
- `Process`: `ProcessDefinition`と、実行中の`ProcessInstance`
- `State`: version履歴を持つ現在状態
- `Context`: activationごとにMemoryから再構築する読み取り専用view
- `Continuation`: Eventを待つ論理的な再開条件
- `Runtime`: 配送・実行・永続化を束ねる機構

Project、Agent、Knowledge、Resourceはdomain recordです。新しいcore primitiveでは
ありません。

## Project Orchestrator

通常のrequestはすべて`ProjectOrchestrator.submit()`へ入り、Routerが次のいずれかを
選びます。

- 新しいProjectを作る
- 既存ProjectへTaskを追加する
- 既存Projectの状況を更新する
- 既に扱われているため無視する

Projectには同時に1つのAgentだけを割り当てます。Project同士は親子関係を持てますが、
Agent同士を直接会話させません。調整は常にOrchestratorを通ります。

Agentの完了報告は即時完了ではありません。

```text
Agent COMPLETED
      ↓
Project WAITING_REVIEW
      ↓
Artifact PENDING_REVIEW
      ├─ approve → APPROVED → Project COMPLETED
      └─ reject  → REJECTED → 同じProject・同じAgentへ修正Task
```

承認と差し戻しはCockpitから行い、Knowledge revisionとして追記します。request keyで
二重クリックを抑止し、再起動後も同じProjectとAgentへ復帰します。

## Knowledge Runtime

Knowledge Ledgerはappend-onlyです。修正、annotation、relation、統合memory、principle、
prediction、reviewのいずれも過去行を上書きせず、新しいrevisionとして追加します。

KnowledgeとWorld Viewは別物です。

```text
Raw Knowledge
  ├─ annotation / relation
  ├─ consolidated memory
  ├─ principle / prediction
  └─ world_fact annotation ──→ World Projection
                                      ↓
                           Gap / Risk / Opportunity
                                      ↓
                            Orchestrator.submit()
```

World ProjectionはLedgerから再構築できます。検出器がProjectを直接作ることはなく、必ず
人間のrequestと同じOrchestrator公開入口を通ります。

Autonomous Knowledge loopが自動実行できるのは、設定されたlow-risk、high-confidence、
read-onlyの範囲だけです。それ以外はCockpitで人間の確認を待ちます。

## Agent Runtime

現在は2つのtransportがあります。

- `InProcessAgentRuntime`: テストとローカル利用向け。与えられたEvidenceだけを分析し、
  PCや外部Toolを直接操作しないbounded Agent
- `A2AAgentRuntime`: Project全体を外部Agentへ委譲し、A2A messageを監査記録するtransport

Agent RuntimeはOrchestratorのinterfaceの背後にあるため、将来transportを追加しても
ProjectとKnowledgeのモデルは変わりません。

## Durable Runtime

補助的なevent-driven runtimeは次のloopを提供します。

```text
Eventを永続化
  → EventDeliveryを同じtransactionで作成
  → RouterがProcessを起動または再開
  → ExecutorがProcessResultをatomic commit
  → 新しいEventを永続化
```

Eventの保存と配送は別の状態です。未配送Eventは再起動後に回収されます。同じEventが
再配送されても`trigger_event_id`により同じProcessは二重起動しません。

Processが中断すると、Python call stackではなく`Continuation`の`resume_point`、
`waiting_for`、`saved_process_state`をSQLiteへ保存します。再起動後は現在のMemoryから
Contextを再コンパイルして再開します。

既存DBに、削除済み機能のtableやProcessDefinitionが残っていても破壊しません。旧tableは
履歴として保持し、handlerが現在のregistryにないProcessDefinitionは新規起動も再開も
行いません。新規DBには現行機能のtableだけを作成します。

## IngressとResource

外部情報はIngress境界だけからEventになります。

- adapterが`source_event_key`を決める
- `(adapter_id, source_event_key)`のDB制約でredeliveryをdeduplicateする
- receipt、Event、checkpointをatomicに保存する
- adapterは内容を解釈せず、外部位置はCheckpointへ保存する

永続的な外部物は次の3段階で管理します。

```text
Resource            何であるか、URIによる同一性
ResourceVersion     その時点で含んでいたimmutableなbytes
Representation      特定Versionから抽出したtext / structure / metadata
```

抽出はRuntime機能ではなく通常のProcessです。Context Snapshotには実際に読んだ
ResourceVersionとRepresentationを記録します。

## SQLiteの現行データ

現行schemaは次の領域だけを作成します。

- Eventと配送: `events`, `event_deliveries`
- Process: `process_definitions`, `process_instances`, `process_activations`
- 再開: `continuations`, `timers`, `joins`
- Context/State: `context_snapshots`, `world_state_history`, `world_state_current`
- Ingress/Resource: `ingress_receipts`, `adapter_checkpoints`, `resources`,
  `resource_versions`, `resource_representations`
- Project: `orchestrator_projects`, `orchestrator_agents`,
  `orchestrator_a2a_messages`, `orchestrator_instructions`
- UI/Knowledge: `project_chat_threads`, `project_chat_messages`,
  `knowledge_revisions`

## Source layout

```text
nexus_seed/
├── core/           # 6 primitivesのpure data model
├── runtime/        # router, scheduler, executor, dispatcher
├── storage/        # sqlite3 store
├── context/        # activationごとのContext compiler
├── delivery/       # durable EventDelivery
├── ingress/        # external observation boundary
├── adapters/       # manual, webhook, local file
├── resources/      # Resource / Version / Representation
├── processes/      # 現行の通常Process handler
├── knowledge/      # append-only ledgerとWorld Projection
├── orchestrator/   # Project / Agent / A2A lifecycle
├── providers/      # A2A transport client
├── chat/           # Project-scoped conversation
└── cockpit/        # human review UI/API
```

## 検証

```bash
pytest
python -m nexus_seed.app --once
```

テストは一時SQLite DBを使い、restart、redelivery、重複review、Resource provenance、
Project/Agent reconcileを含めて確認します。
