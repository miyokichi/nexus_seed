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
- 再起動時にreconcileされる、Project・Agent・委譲状態の永続化
- テストやローカル統合向けの決定的な`InProcessAgentRuntime`
- Project全体を実際の外部AgentへA2Aで委譲する`A2AAgentRuntime`
- `NEXUS_SEED_PROJECT_ORCHESTRATOR_ENABLED`により、`nexus-seed task`・webhook・
  各種connectorからの通常requestをProjectへrouting
- Cockpitの**Projects**画面（orchestrator自身のrecordを表示）

今回も意図的に作っていないもの: 1 Projectへの複数Agent、Agent同士の直接通信、
そして`orchestrator_projects`と従来のGoal由来projectionの統合です。2種類のProjectは
統合せず、別々に表示します。

従来のdurable runtimeは互換applicationとしてリポジトリに残り、テストも維持されています。
各moduleの`KEEP`、`MOVE_TO_AGENT_RUNTIME`、`DEPRECATE`の分類は
[再設計の棚卸し](docs/orchestrator-redesign-inventory.md)を参照してください。この再設計では
既存moduleを削除していません。

## Knowledge Runtime

Project Orchestratorの上位には、NEXUS SEEDが得た情報をversion管理・provenance付きで
記録し、そこからOrchestratorのworld viewを導出するKnowledge Runtime（`nexus_seed/knowledge/`）が
あります。

```text
Knowledge Runtime      世界をどう認識しているか
Project Orchestrator   それに対して何をすべきか
Agent Runtime          どう実行するか
```

- append-onlyなKnowledge Ledger — 上書き・削除は一切なく、修正・annotation・relationは
  常に新しいrevisionとして追加され、transaction time・valid time（後から届いた過去の証拠を
  含む）の両方で問い合わせ可能
- Ledgerから構築されるWorld Projection。既存のWorld State APIとは独立しつつ読み取り互換
- 関連するKnowledgeを`consolidated_memory`へ圧縮するMemory Consolidation。元Knowledgeは
  削除せず、解決できない矛盾を無理に解決しない
- 複数事例からcandidate principleを一般化し、反例で検証し、実際の結果に対する予測器として
  評価するPrinciple Extraction
- 検出したGap/Risk/Opportunityを、既存の`ProjectOrchestrator.submit()`経由の通常requestへ
  変換するGoal bridge（Projectを直接作成することはありません）

現時点ではlibraryとして利用可能で、専用のtest一式（`tests/test_knowledge_*.py`）があります。
`app.py`・demo・Cockpitへの組み込みはまだ行っていません。詳細は
[アーキテクチャ詳細（日本語）](docs/architecture.ja.md)と[AGENTS.md](AGENTS.md)（英語）の
不変条件を参照してください。

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

`nexus-seed project`は明示的な入口なので既定でProjectの完了まで待ちます。常駐時と
同じく受付だけで返す場合は`--no-wait`を付けてください。

このコマンドはdatabaseを直接操作します。同じdata directoryでNEXUS SEEDを常駐させて
いる場合は`nexus-seed task`を使ってください。1つのProjectをreconcileするのは1 process
という前提であり、2つあると同じAgentへ同じ問い合わせを行うことになります。

## 通常運転

Project Orchestratorを有効にすると、通常のrequestがProjectになります。NEXUS SEEDを
常駐させ、これまでどおりの入口から依頼します。

```dotenv
NEXUS_SEED_PROJECT_ORCHESTRATOR_ENABLED=true
```

```powershell
nexus-seed                                        # 常駐起動
nexus-seed task "samples/sample_sales.csvを分析して2026年7月の売上低下原因を調べて"
```

`task`はrequestを受け付けた時点で返ります。Projectは必要なだけ時間がかかるためで、
以降の処理は常駐側のtickで進みます。

```text
CLI / webhook / connector -> Ingress -> human_message -> ProjectRouter
                                                      -> Project + Agent
                                                      -> A2Aへ委譲
```

委譲は永続化されます。どのgoal/taskを渡したか、remote task id、試行回数、次回retry時刻を
SQLiteへ記録するため、実行途中でNEXUS SEEDを停止しても失われません。次回起動時に同じ
Agentをre-adoptし、停止中に起きたことを回収します。Agent Runtimeへ到達できない場合は
上限付きでretryし、それ以上は追わずに放置します。Projectのblockerにはしません。

状況はCockpitの**Projects**画面（`/cockpit`）で確認できます。従来のGoal由来projectionは
**Goal Projects**として残しています。同じ「Project」という語でも別物なので、統合せず
分けて表示します。

なお`nexus-seed task`は従来どおりWorld Stateの解釈経路も通ります。このflagはProjectへの
経路を追加するものであり、知覚を止めるものではありません。

### BLOCKED Projectの再開

Agentが自力で続行できない場合はescalationを返し、Projectはその理由とともにblockされます。
解除は依頼と同じ入口から行います。

```powershell
nexus-seed task "sales.csvを分析し、さらにSAPから前年同期データを取得して比較して"
# -> NEED_RESOURCE: SAPデータが無い -> BLOCKED

nexus-seed task "SAPは使わなくていい。今ある2026年6月データだけで分析を続けて"
# -> ADD_TASK_TO_PROJECT（同じProject・同じAgent）-> ACTIVE -> COMPLETED
```

ProjectRouterにはactiveなProjectだけでなくBLOCKED・WAITING_HUMANのProjectも渡します。
人からの回答が新規Project扱いにならず、待っているProjectへ戻るためです。blockerは解除時に
削除せず、解除時刻と解除理由を記録して履歴として残します。

これはNEXUS SEEDが自ら行う唯一の判断なので、routing用modelが
`NEXUS_SEED_LLM_TIMEOUT_SECONDS`以内に応答することが前提になります。応答しない場合は
新規Project作成へfallbackし（無関係なProjectへ誤って追加するより安全なため）、その理由を
記録します。follow-upが新規Projectになり続ける場合は、この timeout を延ばすか
（大きなlocal modelでは数分かかることがあります）、routingに速いmodelを使ってください。

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
6つのままです。Project、Agent、Skill、Work、Capability、Knowledgeはdomain recordまたは
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
nexus_seed/knowledge/     Knowledge Ledger、World Projection、Consolidation、Principle
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
A2A境界のtestはlocalのscripted HTTP serverを使います（永続化された委譲、上限付きretry、
再起動時のreconcileを含みます）。実Agentが必要なend-to-end testだけが
`tests/integration/`にあり、Agentを指定しない限りskipされます。

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
