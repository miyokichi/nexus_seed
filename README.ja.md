# NEXUS SEED

NEXUS SEEDは、永続化された**Project Orchestrator**です。どのProjectを存在させるかを判断し、
各Projectに1つのAgentを割り当て、Agentから返る結果やエスカレーションを処理します。

```text
ContextManager -> ProjectRouter -> ProjectManager -> AgentManager -> A2AGateway
```

Projectの識別子・優先度・ライフサイクル・担当Agent・blocker・監査可能なAgent通信は
NEXUS SEEDが管理します。Task分解、Capability選択、Tool利用、実作業はAgentが管理します。

*[English](README.md)*

## 現在の実装状況

新しいProject OrchestratorはPython APIとして利用でき、次を実装済みです。

- Project・Agent・A2A messageのSQLite永続化
- 新規Project作成、既存ProjectへのTask追加、Project更新、無視を選ぶsemantic routing
- **1 Project = 1 Agent** の不変条件
- 完了、進捗、blocker、人への確認、新規Project発見の監査付き処理
- 再起動後も復元できるProject・Agent状態
- テストやローカル統合向けの決定的な`InProcessAgentRuntime`
- Project全体を実際の外部AgentへA2Aで委譲する`A2AAgentRuntime`
  （`nexus-seed project "<request>"`から利用）

まだ完成扱いでない統合境界は1つです。webhook serverとCockpitは現在も再設計前の
event-processing applicationを起動するため、`ProjectOrchestrator`の入口には
なっていません。

従来のdurable runtimeは互換applicationとしてリポジトリに残り、テストも維持されています。
各moduleの`KEEP`、`MOVE_TO_AGENT_RUNTIME`、`DEPRECATE`の分類は
[再設計の棚卸し](docs/orchestrator-redesign-inventory.md)を参照してください。この再設計では
既存moduleを削除していません。

## 処理の流れ

```text
外部からのrequest
      |
      v
routing contextを構築（active Project + World State + user context）
      |
      v
新規Project / Task追加 / Project更新 / 無視 を判断
      |
      v
Project専属Agentを新規割り当て、または再利用
      |
      v
監査付きA2A gatewayからGoalまたはTaskを委譲
      |
      v
完了、進捗更新、またはエスカレーション待ち
```

AgentからNEXUS SEEDへ返すmessageは、Project管理に必要なものだけです。

| Message | NEXUS SEED側の処理 |
| --- | --- |
| `PROJECT_STATUS` | Projectの最新summaryを記録 |
| `PROJECT_COMPLETED` | Projectを完了し、Agentをidleへ変更 |
| `NEED_CAPABILITY` | 不足Capabilityをblockerとして記録 |
| `NEED_RESOURCE` | 不足Resourceをblockerとして記録 |
| `NEED_PERMISSION` | 不足Permissionをblockerとして記録 |
| `PROJECT_BLOCKED` | その他のblockerを記録 |
| `NEED_HUMAN_INPUT` | Projectを`WAITING_HUMAN`へ変更 |
| `DISCOVERED_NEW_PROJECT` | 発見内容を新しいrequestとしてrouting |

Projectを作成できるのはNEXUS SEEDだけです。Agentは新しい問題を報告できますが、
Projectを直接作成することはできません。

## インストール

Python 3.12以上が必要です。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## Project Orchestratorを試す

次の例はnetworkを使わないAgent runtimeを利用します。routing backendを渡さない場合は、
安全なfallbackとしてrequestごとに新しいProjectを作成します。

```python
import asyncio

from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator


async def main() -> None:
    orchestrator = ProjectOrchestrator(
        "nexus.db",
        agent_runtime=InProcessAgentRuntime(),
    )
    try:
        decision = await orchestrator.handle_request("7月の売上低下原因を調べて")
        project = orchestrator.projects.all()[0]
        print(decision.action.value, project.id, project.status.value)
    finally:
        orchestrator.close()


asyncio.run(main())
```

現在のProject一覧を考慮したsemantic routingには、`backend=`へ`ExecutionBackend`を渡します。
Project Agentをどこで動かすかは、`agent_runtime=`へ渡す`AgentRuntime`で決まります。
`InProcessAgentRuntime`はscripted fakeで、`A2AAgentRuntime`は次節のとおり実際の
外部Agentへ委譲します。

## 実Agentでの実行

`nexus-seed project`が新しいProject Orchestratorの入口です。`nexus-seed task`とは
別の入口として維持します。

```text
nexus-seed task     -> 従来のdurable Goal / Work runtime
nexus-seed project  -> 新しいProjectOrchestrator
```

外部Agent Runtimeを別terminalで起動します。A2Aを話すAgentであれば何でも構いません。
次の例はLittle Agentのprofileを、読み書きできるworkspace付きで起動しています。

```powershell
$env:LITTLE_AGENT_WORKSPACE = "C:/work/project-agent"
little-agent --serve-a2a --agent analysis_worker --port 8801 --auto-approve
```

NEXUS SEED側の設定は次のとおりです。

```dotenv
NEXUS_SEED_PROJECT_AGENT_RUNTIME=a2a
NEXUS_SEED_PROJECT_AGENT_URL=http://127.0.0.1:8801
# 任意。bearer tokenを保持する環境変数名を指定します。
NEXUS_SEED_PROJECT_AGENT_TOKEN_ENV=LITTLE_AGENT_A2A_TOKEN
# 任意。これを超えるとAgentへ到達できないものとして扱います。
NEXUS_SEED_PROJECT_AGENT_TIMEOUT_SECONDS=1200
# 任意。Agentが書き込めるworkspace。Projectごとに1 directoryを渡します。
NEXUS_SEED_PROJECT_WORKSPACE=projects
```

これで依頼を投入できます。

```powershell
nexus-seed project "samples/sample_sales.csvを分析して2026年7月の売上低下原因を調べて"
```

```text
Routing: CREATE_PROJECT
Project: project-8a49c176-74c4-4bc5-9725-c10236e81205
Agent:   agent-f00bda11-396c-4823-a595-bd5d56a99666
Status:  COMPLETED
Goal:    Analyze sales data to identify reasons for low sales in July 2026 ...
Summary: ~92% of the revenue drop is concentrated in one store/category ...
```

NEXUS SEEDが渡すのはgoal、context、constraints、workspace、そして`skills/`にある
Skillのcontractまでです。Task分解、順序、どのSkillを使うかはProject Agentが決めます。
自力で続行できない場合はretryを繰り返さずescalationを返し、Projectはその理由とともに
blockされます。

```text
Status:  BLOCKED
Blocked: NEED_RESOURCE - SAPの履歴fileも前年同期の行も存在せず、
         捏造なしには比較できない
```

Agent Runtimeが停止している状態と、Projectが遂行できない状態は別物として扱います。
前者ではProjectの状態を維持し、失敗はAgent側に記録され、もう一度コマンドを実行すれば
再委譲されます。

`NEXUS_SEED_PROJECT_AGENT_RUNTIME`を未設定（または`in_process`）にすれば決定的な
runtimeで動きます。orchestration自体はどちらでも同一です。

## 互換application

新しいorchestratorの統合中も、従来のevent-processing applicationは利用できます。
source tree外のdata directoryとwebhook tokenを設定してください。

```dotenv
NEXUS_SEED_DATA_DIR=C:/Users/user/AppData/Local/nexus-seed
NEXUS_SEED_WEBHOOK_HOST=127.0.0.1
NEXUS_SEED_WEBHOOK_PORT=8787
NEXUS_SEED_WEBHOOK_TOKEN=replace-this-token
NEXUS_SEED_COCKPIT_ENABLED=true
```

起動方法は次のとおりです。

```powershell
nexus-seed --once
nexus-seed
```

互換Cockpitは`http://127.0.0.1:8787/cockpit`で開きます。主なコマンドは次のとおりです。

```powershell
nexus-seed status
nexus-seed task "この依頼を分析して"
nexus-seed reviews
nexus-seed review <review-id> approve
nexus-seed control '/status'
```

これらのコマンドが操作するのは、従来のdurable Goal / Work / Capability runtimeです。
新しい`ProjectOrchestrator` APIではありません。

## 設計境界

固定primitiveは`Event`、`Process`、`State`、`Context`、`Continuation`、`Runtime`の
6つのままです。Project、Agent、Skill、Work、Capabilityはdomain recordまたは
Processの役割であり、新しいprimitiveではありません。

再設計では次の責務を分離します。

```text
Project     NEXUS SEEDが存在を判断し、進行を管理する対象
Agent       1つのProjectの実行を担当する主体
Skill       再利用可能な認知手順
Capability  何ができるか
Work        Project内で何をする必要があるか
Provider    どこで実行するか
```

durabilityはNEXUS SEED、executionはProject Agentの責務です。AgentはA2A経由で、
Project管理に必要なstatusとescalationだけを返します。

## ディレクトリ構成

```text
nexus_seed/orchestrator/  Project routing、lifecycle、Agent割り当て、A2A
nexus_seed/storage/       orchestrator recordを含むSQLite store
nexus_seed/core/          固定された6つのdata model
nexus_seed/runtime/       従来のdurable event runtime
nexus_seed/processes/     従来のProcess handler
nexus_seed/providers/     Provider federation、A2A client、Project Agent transport
nexus_seed/control/       互換Command、Human identity、Goal
nexus_seed/cockpit/       互換read modelと依存なしのWeb UI
skills/                   directory Skill（skill.json + SKILL.md）
tests/                    unit、acceptance、restart convergence test
```

## 開発・確認

```powershell
pytest
python -m nexus_seed.demo
```

`pytest`は外部Agentを必要としません。orchestratorのtestは`InProcessAgentRuntime`を、
A2A境界のtestはlocalのscripted HTTP serverを使います。実Agentが必要なend-to-end test
だけが`tests/integration/`にあり、Agentを指定しない限りskipされます。

```powershell
$env:NEXUS_SEED_PROJECT_AGENT_URL = "http://127.0.0.1:8801"
pytest tests/integration
```

## 詳細資料

- [Project Orchestrator再設計の棚卸し](docs/orchestrator-redesign-inventory.md)
- [ArchitectureとPhase履歴（英語）](docs/architecture.md)
- [機能棚卸し（英語）](docs/architecture-inventory.md)
- [アーキテクチャ詳細（日本語）](docs/architecture.ja.md)
- [機能棚卸し（日本語）](docs/architecture-inventory.ja.md)
- [開発時の不変条件と作業規約](AGENTS.md)
