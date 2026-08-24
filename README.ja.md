# NEXUS SEED

NEXUS SEEDは、永続化された**Project Orchestrator**です。どのProjectを存在させるかを判断し、
各Projectに1つのAgentを割り当て、Agentから返る結果やエスカレーションを処理します。

```text
ContextManager -> ProjectRouter -> ProjectManager -> AgentManager -> A2AGateway
```

Projectの識別子・優先度・ライフサイクル・担当Agent・blocker・監査可能なAgent通信は
NEXUS SEEDが管理します。Task分解、Capability選択、Tool利用、実作業はAgentが管理します。

*[English](README.md)*

## クイックスタート

### 1. インストール

Python 3.12以上が必要です。`uv`を使う場合は次のとおりです。

```bash
uv sync --extra dev
cp .env.example .env
```

Windows PowerShellでは、設定ファイルのコピーは次のように行います。

```powershell
uv sync --extra dev
Copy-Item .env.example .env
```

`uv`を使わない場合は、virtual environmentを作成してeditable installできます。

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

以降、`uv`を使う場合は各commandを`uv run nexus-seed ...`として実行してください。
editable install済みのenvironmentでは`nexus-seed ...`だけで実行できます。

### 2. 最小設定

`.env`で少なくとも次を確認してください。data directoryはsource repositoryの外を
指定します。空欄の場合は`~/.nexus_seed`が使われます。

```dotenv
NEXUS_SEED_DATA_DIR=/path/to/nexus-seed-data
NEXUS_SEED_WEBHOOK_HOST=127.0.0.1
NEXUS_SEED_WEBHOOK_PORT=8787
NEXUS_SEED_WEBHOOK_TOKEN=change-this-token
NEXUS_SEED_COCKPIT_ENABLED=true
NEXUS_SEED_KNOWLEDGE_LOOP_ENABLED=true
NEXUS_SEED_PROJECT_AGENT_RUNTIME=in_process
```

Windowsでは、例えば`NEXUS_SEED_DATA_DIR=C:/Users/USER/AppData/Local/nexus-seed`
のように指定できます。

NEXUS SEED自身に状況評価やsemantic routingをさせる場合はLLMも設定します。
OpenAI互換のlocal serverを使う例です。

```dotenv
NEXUS_SEED_LLM_ENABLED=true
NEXUS_SEED_LLM_PROVIDER=openai_compatible
NEXUS_SEED_LLM_BASE_URL=http://127.0.0.1:1234/v1
NEXUS_SEED_LLM_MODEL=local-model
NEXUS_SEED_LLM_API_KEY_ENV=OPENAI_API_KEY
OPENAI_API_KEY=
```

設定内容はsecretを表示せず確認できます。

```bash
nexus-seed config
nexus-seed --check-llm  # LLMを有効にした場合だけ
```

LLMを無効にしても、永続Runtime、Ingress、Knowledge記録、Cockpit、Project管理は
動作します。ただしSituation EvaluatorはProject Proposalを生成せず、現在の
`in_process` Agentは成果を推測せずに人へ確認を求めます。

### 3. 起動と終了

通常運転ではserverを1つ起動したままにします。

```bash
nexus-seed
# 同等: nexus-seed serve
```

起動後にブラウザで次を開きます。

```text
http://127.0.0.1:8787/cockpit
```

token入力を求められたら、`.env`の`NEXUS_SEED_WEBHOOK_TOKEN`を入力します。終了は
terminalで`Ctrl+C`です。Project、Knowledge、観測checkpoint、review判断はSQLiteへ
保存されているため、同じ`.env`で再起動すれば途中から再開します。

serverを起動せず、回収可能な処理を一度だけ進めて終了するには次を使います。

```bash
nexus-seed --once
```

### 4. Cockpitの使い方

Cockpitには次の画面があります。

| 画面 | 用途 |
| --- | --- |
| **Overview** | runtime、Project、review待ち、エラーの概要 |
| **World** | 観測元、World facts、Proposal、質問、Entity、成果物の確認 |
| **Activity** | 最近のEvent・Process・Project活動 |
| **Projects** | Projectの状態、担当Agent、blocker、A2A履歴、追加指示 |
| **System** | durable event deliveryとProcess failureの診断情報 |

#### PC環境を明示的に観測する

PC情報は自動では読み取りません。**World → Observation Sources**で次の操作をします。

1. 観測元の名前と観測間隔を入力する。
2. 読み取りを許可する項目だけを選ぶ。
3. **登録して観測**を押す。

選択できる項目は、OS、ホスト名、CPU論理数、物理メモリ量、
`NEXUS_SEED_DATA_DIR`のdisk使用量だけです。process一覧、任意のfile内容、user directoryを
暗黙に走査することはありません。

登録後は、次の操作ができます。

- **今すぐ観測** — polling間隔を待たずに読み取る
- **停止** — 設定と履歴を残したまま自動観測を止める
- **再開** — 同じ観測元を再び有効にする

初回と値が変化したときだけ`system_snapshot_observed` EventがIngressを通ります。
値が同じ場合はKnowledgeを重複生成しません。観測元、差分判定checkpoint、最終Event、
エラーは再起動後も保持されます。1つの観測元が失敗しても、他の観測元は継続します。

#### フォルダの「溜まり具合」を観測する

同じ**World → Observation Sources**から、フォルダの状況を観測元として登録できます。
file監視が「このfileが変わった」を伝えるのに対し、こちらは「何件あるか、合計何バイトか、
最も古い更新はいつか、拡張子ごとに何件か」という*状況*を伝えます。何も編集されなかった
日でも、溜まっていること自体は世界についての事実です。

読むのはフォルダの目録（名前・サイズ・更新日時）だけで、**fileを開くことはありません**。
ここでフォルダを許可しても、中身を読む許可にはなりません（中身の取り込みは
`NEXUS_SEED_DATA_DIR/resources`の別の仕組みです）。CLIからも登録できます。

```bash
nexus-seed-knowledge watch-folder --db k.db /path/to/inbox --name 受信箱
```

#### fileをKnowledgeへ取り込む

起動時に次のdirectoryが作成され、継続監視されます。

```text
NEXUS_SEED_DATA_DIR/resources
```

この中へplain text、Markdown、JSON、CSV、PowerPoint（`.pptx`）、Excel（`.xlsx`）、
Word（`.docx`）を置くと、fileの作成・変更がIngressを通り、Resource、
immutableなResourceVersion、抽出Representationとして記録された後、Knowledge loopへ
入ります。同じ内容は重複versionになりません。directory外のfileは監視しません。
Office形式を読むには`pip install -e '.[ingest]'`が必要です（core依存ではありません）。

#### 手動で状況を伝える

**World → Record Observation**へ自由文を入力します。入力は直接World factになるのではなく、
まず出典付きKnowledgeとして保存されます。LLMが有効ならSituation Evaluatorが現在の
World Viewと進行中Projectを比較し、必要に応じてProject Proposalを作ります。

#### 人による確認

| Cockpit上の項目 | 操作後の挙動 |
| --- | --- |
| Project Proposal | 承認するとProjectへrouting、拒否すると実行しない |
| Artifact | 承認すると`APPROVED`、差し戻すと理由を同じProject・同じAgentへ返す |
| Completion Review | 承認するとProject完了、差し戻すと同じAgentが作業を再開 |
| Question | 回答をKnowledgeへ記録し、待っているProjectへ返す |
| Unknown Entity | canonical Entity IDを確認するか、別Entityとして確定する |

ArtifactとCompletion Reviewは初め`PENDING_REVIEW`です。判断はKnowledge revisionとして
永続化されるため、二重clickや再起動で同じ指示が重複することはありません。Unknown Entityを
canonical IDとして確認すると、関連Observationもそのcanonical EntityのWorld factとして
再投影されます。

### 5. 依頼を出す

常駐serverへ依頼する通常の入口は`task`です。受付後すぐに戻り、処理状況はCockpitで
確認します。

```bash
nexus-seed task "resources/sales.csvを分析し、売上低下の要因をまとめて"
```

同じ外部requestの再送を重複させたくない場合は安定したsource keyを渡します。

```bash
nexus-seed task "売上低下の要因をまとめて" --source-key request-2026-08-24-001
```

serverを起動していない状態でProjectを直接作成し、結果を待つには`project`を使います。

```bash
nexus-seed project "売上低下の要因をまとめて"
nexus-seed project "時間のかかる調査" --no-wait
nexus-seed project "結果をJSONで取得" --json
```

`project`はSQLiteを直接開くため、同じdata directoryで常駐serverが動いている間は使わず、
`task`を使用してください。現在状態の読み取りには次を使えます。

```bash
nexus-seed status
nexus-seed status --json
```

### 6. 自律loopで何が起こるか

```text
System Snapshot / resources / manual input
                 |
                 v
        Ingress（外部identityで重複排除）
                 |
                 v
      Knowledge Ledger + World Projection
                 |
                 v
       Situation Evaluator（LLM、任意）
                 |
                 v
       Project Proposal + deterministic policy
                 |
          +------+------+
          |             |
     low-risk auto   human review
          |             |
          +------+------+
                 v
       Project Orchestrator -> 1 Project / 1 Agent
                 |
                 v
      result / artifact / question / escalation
                 |
                 +---------> Knowledgeへ戻り、次の評価へ
```

自動承認されるのは、証拠があり、confidenceが十分高い、low-riskかつread-onlyのProposalだけ
です。それ以外はWorld画面で人の判断を待ちます。Runtimeやbackendにはこの判断を置かず、
Knowledge loopのdeterministic policyが決定します。

### 7. 外部A2A Agentへ実作業を委譲する

`in_process` AgentはProject contextの推論だけを行い、fileやtoolを直接操作しません。
実作業をさせる場合はA2A対応Agent Runtimeを別processで起動し、`.env`を切り替えます。

```dotenv
NEXUS_SEED_PROJECT_AGENT_RUNTIME=a2a
NEXUS_SEED_PROJECT_AGENT_URL=http://127.0.0.1:8801
NEXUS_SEED_PROJECT_AGENT_TOKEN_ENV=
NEXUS_SEED_PROJECT_AGENT_TIMEOUT_SECONDS=900
NEXUS_SEED_PROJECT_WORKSPACE=projects
```

変更後にNEXUS SEEDを再起動します。NEXUS SEEDはProjectのgoal、context、constraints、
workspaceを渡し、AgentがTask分解、Skill、toolを選びます。AgentのSkill inventoryは
NEXUS SEED側へ複製しません。詳しい起動例は[実Agentでの実行](#実agentでの実行)を参照してください。

### 8. よくある問題

| 状況 | 確認すること |
| --- | --- |
| Cockpitが`401`になる | `.env`と画面に入力した`NEXUS_SEED_WEBHOOK_TOKEN`が同じか |
| Cockpitを開けない | serverが起動中か、host/portが一致するか、`NEXUS_SEED_COCKPIT_ENABLED=true`か |
| Knowledgeは増えるがProposalが出ない | `NEXUS_SEED_LLM_ENABLED`とLLM接続。提案不要という評価も正常 |
| `in_process` Agentが人へ確認を求める | tool/file操作をしないbounded Agentの仕様。実作業にはA2A Agentを設定 |
| fileが取り込まれない | `NEXUS_SEED_DATA_DIR/resources`内か、Knowledge loopが有効か、形式が対応済みか |
| System Snapshotを押してもEventが増えない | 前回と値が同じ場合は正常に重複抑止される |
| follow-upが別Projectになる | routing LLMの接続・timeoutと、元ProjectがCockpitに残っているか |

より細かな設定値は[`.env.example`](.env.example)、内部境界は
[アーキテクチャ詳細](docs/architecture.ja.md)を参照してください。

## 現在の実装状況

新しいProject OrchestratorはPython APIとして利用でき、次を実装済みです。

- Project・Agent・A2A messageのSQLite永続化
- 新規Project作成、既存ProjectへのTask追加、Project更新、無視を選ぶsemantic routing
- **1 Project = 1 Agent** の不変条件
- 完了、進捗、blocker、人への確認、新規Project発見の監査付き処理
- 再起動時にreconcileされる、Project・Agent・委譲状態の永続化
- テストやローカル統合向けの決定的な`InProcessAgentRuntime`
- Project全体を実際の外部AgentへA2Aで委譲する`A2AAgentRuntime`
- `nexus-seed task`・webhook・各種connectorからの通常requestを常にProjectへrouting
- Cockpitの**Projects**画面（orchestrator自身のrecordを表示）

今回も意図的に作っていないものは、1 Projectへの複数AgentとAgent同士の直接通信です。
旧Action／Work／Capability／Planning／自己拡張pipelineはProject Agent側の責務となったため
削除済みです。Event配信、Process再開、Ingress、Resource、Contextなど、Orchestratorと
Knowledge loopが利用するdurable runtime機構だけを残しています。既存SQLite DBに残る旧tableは
履歴として保持されますが、新規DBには作成されません。

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
- 検出したGap/Risk/Opportunityを、他と同じautonomy policyで判断されるproject proposalへ
  変換するGoal bridge（Projectを直接作成することはありません）
- operatorが明示的に許可した観測元: PC自身の固定項目と、フォルダの「溜まり具合」
  （件数・合計サイズ・最古の更新日時）。読むのは目録だけで、fileは開きません

library（`nexus_seed.knowledge`）、専用CLI（`nexus-seed-knowledge`）に加え、
applicationではautonomous Knowledge loopとしても
利用できます。許可された`NEXUS_SEED_DATA_DIR/resources`とCockpitの手動観測が
Knowledgeになり、Situation EvaluatorがProject Proposalを作り、低risk・高confidence・
read-onlyだけを自動実行します。それ以外はCockpitのWorld画面で人間を待ちます。
Agentの成果物・観測・質問は再びKnowledgeへ戻ります。Agentが完了を報告してもProjectは
すぐには完了せず、`WAITING_REVIEW`で停止します。成果物は`PENDING_REVIEW`として保存され、
Cockpitで承認すると`APPROVED`になってProjectが完了します。差し戻すと`REJECTED`になり、
理由を含む修正Taskが同じProject・同じAgentへ返されます。承認・差し戻しはKnowledgeの
新しいrevisionとして残るため、再起動や二重操作でも判断と作業は重複しません。詳細は
[アーキテクチャ詳細（日本語）](docs/architecture.ja.md)と[AGENTS.md](AGENTS.md)（英語）の
不変条件を参照してください。

loopの各passは、判断する前に読み直します。

```text
evidence -> 統合 -> 原則抽出 -> 評価 -> 提案 -> Project
```

- **統合は遅延・主題単位**。ある主題について、まだどのmemoryにも含まれていない観測が
  一定数たまった時点で圧縮します。1 passで圧縮するのは最大1〜2主題です。主題を持たない
  Knowledge（手入力のメモなど）も共通の1バケットにまとめて圧縮するので、読まれないまま
  積み上がることはありません。生成されたmemoryはそれ自体がevidenceになります —
  圧縮する意味はそこにあります。
- **原則は「消化済みの素材」から一般化**します（consolidated memoryと記録済みexperience。
  生の観測1件からは作りません）。評価器に渡されるのは成熟した原則（`supported`/
  `validated`）だけです。`candidate`はまだ反例検証を通っていないため、判断を動かしません。
- **訂正は読み直されます**。「評価済み」はrevision単位で追跡するので、評価済みの
  観測をreviseすると再び評価器の前に出ます。ただし同じ結論を繰り返しても仕事は
  重複しません — 同じevidenceからの同じobjectiveは1つのproposalのままです。
- **落ち着いたLedgerの再チェックは安価**です。各passは前回到達した位置から前へ進むだけで、
  全件を読み直しません。コストは「新しく届いた量」に比例し、「蓄積した量」には比例しません。
  この位置はキャッシュであってqueueではありません — 再起動でリセットされ、実際に何を見たかは
  永続レコード側が決めます。

LLMを使う2つのstep（統合・原則抽出）はreasoning backendを必要とします。backendが無い場合、
loopは毎tick要約なしのlistingを書き出すのではなく、静かに何もしません。

### Autonomous Knowledge loop

```bash
NEXUS_SEED_KNOWLEDGE_LOOP_ENABLED=true
NEXUS_SEED_PROJECT_AGENT_RUNTIME=in_process
NEXUS_SEED_LLM_ENABLED=true
```

起動後、`NEXUS_SEED_DATA_DIR/resources`だけがlocal inboxとして監視されます。別のuser
directoryを暗黙に読むことはありません。PC環境も自動では読みません。Cockpitの
**World**画面でSystem Snapshot観測元を登録し、OS・ホスト名・CPU・メモリ・
`NEXUS_SEED_DATA_DIR`の使用量から許可した項目だけを継続観測できます。変化がない
snapshotは再投入せず、設定とcheckpointは再起動後も保持されます。同画面では、手動観測、現在の
World facts、Project Proposal、Agentの質問、未解決Entity、成果物、raw Knowledgeを同じ
provenanceから確認できます。Completion ReviewsとArtifactsでは、完了報告全体または
個別成果物を承認・差し戻しできます。

現在の`in_process` Agentは、設定されたNEXUS SEED LLMを使い、Projectに渡されたEvidence
だけを分析するbounded Agentです。直接file/tool操作は行いません。LLMが無効なら、成果を
装わず`NEED_HUMAN_INPUT`になります。将来`NEXUS_SEED_PROJECT_AGENT_RUNTIME=a2a`へ変更しても、
Knowledge loopとProject Orchestratorのinterfaceは変わりません。

### Knowledge Runtimeの使い方（Python）

すべてPythonから直接オブジェクトを呼び出して使えます — 自分のcodeへ
Knowledge Runtimeを組み込みたい場合に向いています。（とにかく動かして
みたいだけなら、後述の「コマンドライン」節が同じことをPythonなしで
できます。）一連の流れを通して書くと、次のようになります。

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
    # 0) SQLite databaseを1つ、その上にKnowledgeStoreとKnowledgeLedgerを1つずつ
    #    用意します。Knowledgeへの書き込みは常にLedger経由です。
    db = Database("nexus_knowledge.db")
    ledger = KnowledgeLedger(KnowledgeStore(db))

    # 1) 生のKnowledgeを、届いたそのままの形（自由文・無schema）で記録します。
    #    `source`はprovenance（どこから来たか）であって、まだ世界の事実ではありません。
    k1 = ledger.record(
        "B案の方がmarginはありそうだが、process追加が必要なのでschedule riskが高い。",
        source_type="meeting",
        source_ref="review_20260820",
    )

    # 2) 特定のKnowledgeを「world_fact」annotation（entity/attribute/value）で
    #    構造化World Viewへ組み入れます。これはk1のcontentを書き換えません —
    #    元の文章はそのまま、追加の読み取りを乗せた新しいrevisionが増えるだけです。
    annotate_world_fact(ledger, k1.knowledge_id, entity="project-A", attribute="risk", value="schedule")

    # 3) 現在のWorld View（既存StateStoreと同じ{entity: {attribute: value}}形式）を
    #    投影し、「before」として保持します。
    view_before = WorldStateProjection(ledger).view()

    # ... 時間が経ち、状況が変わったとします ...
    k2 = ledger.record("A案のDRC riskが顕在化し、process側が追加工程を許容可能とした。", source_type="report")
    annotate_world_fact(ledger, k2.knowledge_id, entity="project-A", attribute="risk", value="none")

    # 4) 2つのWorld Viewの差分を取り、event loopへ流せます
    #    （diff.to_events()の各eventを runtime.submit_event(event) へ）。
    view_after = WorldStateProjection(ledger).view()
    diff = diff_world_views(view_before, view_after)
    for change in diff.changes:
        print(change.entity, change.attribute, change.old_value, "->", change.new_value)

    # 5) 関連する生Knowledgeを1つのmemoryへ統合します。ここではFakeLLMBackendですが、
    #    実際は`LLMBackend`など本物のExecutionBackendを渡します。backendが無くても
    #    consolidate()自体は動作し、その場合は要約を作らず元Knowledgeを一覧するだけに
    #    とどめます（推測で結論を作らない）。
    candidates = select_candidates(ledger, kinds=("raw",))
    consolidate_backend = FakeLLMBackend(script=[proposal_response({
        "summary": "当初B案はschedule risk懸念だったが、A案のDRC riskが顕在化しB案再検討の合理性が高まっている。",
        "unresolved": [],
        "confidence": 0.7,
    })])
    memory = await Consolidator(ledger, consolidate_backend).consolidate(candidates)

    # 6) 2件以上の関連事例からreusableな原則を抽出し、独立した支持が積み重なるまで
    #    record_support()を呼んで成熟度を上げます。
    principle_backend = FakeLLMBackend(script=[proposal_response({
        "principle": "早期の代替案再検討は、主要riskの顕在化に応じて柔軟に行うべきである。",
        "confidence": 0.6,
        "scope": None,
    })])
    principle = await PrincipleExtractor(ledger, principle_backend).extract(candidates)
    principle = record_support(ledger, principle, "evidence-1")
    principle = record_support(ledger, principle, "evidence-2")  # ここで"supported"になる

    # 7) 成熟したprincipleの条件が現在のWorld Viewに一致する箇所から
    #    Gap/Risk/Opportunityを検出します。
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

    # 8) 各signalをproject proposalとして登録します。ここで止まります —
    #    自動で進めてよいかはautonomy policyが判断し、low-risk read-only以外は
    #    人が判断し、承認済みproposalをProject Orchestratorへ渡すのはloopです。
    for signal, proposal in await GoalBridge(ledger).submit(signals):
        print(signal.type, proposal.status, proposal.knowledge_id)


asyncio.run(main())
```

使う前に知っておくとよい点:

- **何も上書き・削除されません。** `ledger.revise(...)`・`ledger.annotate(...)`・
  `ledger.relate(...)`はどれも*新しい*revisionを追加するだけで、
  `ledger.history(k1.knowledge_id)`は常にこれまでの全revisionを返します。
  「その時点で何を信じていたか」は`ledger.as_known_at(id, t)`
  （transaction time）、「その時点で実際に何が真だったか（現時点で分かる限り）」は
  `ledger.valid_at(id, t)`（valid time）で問い合わせます — 両者が食い違うことこそが
  この2つを分けて持つ意味です。
- **書き込み時にschemaを強制しません。** `ledger.record(...)`が必要とするのは
  自由文の`content`と`source_type`/`source_ref`だけです。world-factの読み取りや
  principleのscopeなどの構造は、常に後から追加する任意の`Annotation`/`metadata`です。
- **LLMを使う各stepは、backendが無くても安全に縮退します。** `Consolidator`・
  `PrincipleExtractor`・`CounterexampleSearcher`・`PredictionEngine`・
  `GapRiskOpportunityDetector`はいずれも`backend=None`（既定値）を受け付け、
  その場合は何も返さないか、統合・一般化しない素の回答を返します — 結論・反例・
  business riskを推測で捏造することはありません。LLMを使った挙動が必要な場合は、
  本物の`ExecutionBackend`（`nexus_seed/backends/llm.py`参照）を渡してください。
- **KnowledgeからProjectへの経路は1本だけです**。proposalを作り、autonomy
  policyが判断し、loopが`ProjectOrchestrator.submit()`へ渡します。`GoalBridge`は
  その経路の横に並ぶのではなく、その経路に合流します — proposalを登録して止まる
  ので、principle起点の発見にもevidence起点と同じgate・同じ人間レビュー
  （low-risk read-only以外）・同じexactly-once submissionが適用されます。
  principleを1つも挙げない発見は根拠が無いということなので、実行されず
  `FORBIDDEN`として記録されます。
- より詳しく知りたい場合は`tests/test_knowledge_*.py`を読んでください。各fileが
  1つのphase（K1: revision・temporal query、K2: projection・diff、K3:
  consolidation、K4: principle、K5: Goal bridge、K6: experience・advisory）の
  実行可能な例になっています。

### コマンドライン（`nexus-seed-knowledge`）

ここまでの操作はすべて、Pythonを書かなくても1つのCLI（操作ごとにsubcommand）
から実行できます。

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
nexus-seed-knowledge signals --db k.db          # 検出のみ
nexus-seed-knowledge propose --db k.db          # 検出 + proposalとして登録（routingはloop）
nexus-seed-knowledge advise --db k.db --subject project-A
nexus-seed-knowledge --help                     # 全subcommand（各subcommandにも--help）
```

autonomous loopもここから操作できるので、headless運用がCockpitで届く範囲に
縛られません。

```bash
nexus-seed-knowledge watch-folder --db k.db /path/to/inbox --name 受信箱
nexus-seed-knowledge principles --db k.db       # 何を学んだか、どれだけ支持されているか
nexus-seed-knowledge principles --db k.db --mature-only
nexus-seed-knowledge memories --db k.db         # 何を、何から統合したか
nexus-seed-knowledge reconcile --db k.db        # loopを1 pass実行
nexus-seed-knowledge pending --db k.db          # 人の判断待ちを一覧
nexus-seed-knowledge decide --db k.db K-xxxx approve --note "..."
nexus-seed-knowledge answer --db k.db K-xxxx "自動更新はしません"
```

`decide` はidの実体（proposal / artifact / completion review / 未解決entity）を
見て処理を振り分けるので、どれだったかを覚えておく必要はありません。いずれも
Cockpitが呼ぶのと同じ`KnowledgeLoop`を通ります（実装は二重化していません）。

packageのconsole scriptを入れていない場合は`python -m nexus_seed.knowledge_cli ...`
で実行できます。`--db`は1つのfileをKnowledge LedgerとProject Orchestratorの両方で
共有します（1つのschemaが全tableを作るため）ので、`submit`は検出したSignalを
同じdatabase上の実際のProjectへそのまま渡せます — 別のorchestrator用DBを使いたい
場合だけ`--orchestrator-db`を指定してください。

LLMを使う各subcommand（`consolidate`・`extract-principle`・`counterexample`・
`predict`・`signals`・`submit`）は、NEXUS SEEDの他機能と同じ`NEXUS_SEED_LLM_*`
設定を読みます（`.env.example`参照）— `NEXUS_SEED_LLM_ENABLED=true`を設定するか、
その回だけ`--llm`を渡してください。`--no-llm`は常に上記の安全なfallbackを強制
します。読み書き系commandの`--json`は、1行要約ではなくKnowledge revision全体を
出力します（scripting向け）。

### Officeファイル（.pptx / .xlsx / .docx）を外界の状況として取り込む

file observerは以前から、許可フォルダ配下の全ファイルを種類を問わず監視・
版管理・fingerprint化していました。できなかったのはOfficeファイルを*読む*
ことだけで、そのため変更は検知されても中身が入りませんでした。PowerPoint・
Excel・Word用のExtractorがこの穴を埋めます。

```bash
pip install -e '.[ingest]'   # python-pptx, openpyxl, python-docx（core依存ではありません）
```

監視フォルダにファイルを置けば、次のpollで通常のResource pipelineを通って
中身がKnowledgeになります。別途importする操作は要りません。

| ファイル | Representation | 中身 |
| --- | --- | --- |
| `.pptx` | text | slideごとに1ブロック（`[slide N]`付き） |
| `.xlsx` / `.xlsm` | structure | `{sheet: {columns, rows, row_count}}`。数式は最後にキャッシュされた値 |
| `.docx` | text | 段落、続いて表の行 |

監視中のファイルを編集すると、最初の観測を上書きせず**2件目の観測**として
記録されます。そのdeckが以前何と言っていたかは、今何と言っているかの隣に
残ります。readerはoptionalです。extraを入れていない場合もファイルは版管理
され、抽出は「入れるべきpackage名」を示して失敗するので、後から入れれば
次の変更時に中身を拾います。

監視フォルダを介さない単発の取り込みには `nexus-seed-knowledge pptx` が使え、
各slideを個別のKnowledge objectとして記録します（同じExtractorでslideを読むので
両経路の結果は一致します）。

```bash
nexus-seed-knowledge pptx --db nexus_knowledge.db slide.pptx
nexus-seed-knowledge pptx --db nexus_knowledge.db --dir ./docs --about project-A
# 同等: python -m nexus_seed.knowledge.ingest_pptx --db ... --dir ./docs
```

空でない各slideが1つの`kind=raw`のKnowledge object
（`ledger.record(slide_text, source_type="pptx", source_ref="<file>#slide<N>")`）
として記録され、そのdeckへの`about` relation（既定はfile名、`--about`を
指定すればそれを使うので、同じ主題の複数deckをまとめられます）が付きます。
このrelationはK3の`select_candidates(ledger, about=...)`が絞り込みに使う
ものと同じなので、追加の配線なしにそのまま`Consolidator`・
`PrincipleExtractor`の対象になります。programmaticにも呼べます：

```python
from nexus_seed.knowledge.ingest_pptx import ingest_pptx_file

created = ingest_pptx_file(ledger, Path("review_20260820.pptx"), about="project-A")
```

正式なExtractor統合（`nexus_seed/resources/`経由でのversion管理・重複排除つき
取り込み）は指示があってから着手します。

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

NEXUS SEEDが渡すのはgoal、context、constraints、workspaceです。Task分解、順序、
利用するSkillやtoolはProject Agent自身が決め、NEXUS SEEDはそれらをinventoryしません。
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

通常のrequestは常にProjectになります。NEXUS SEEDを常駐させ、次の入口から依頼します。

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

状況はCockpitの**Projects**画面（`/cockpit`）で確認できます。外部の状況、Project提案、
成果物、完了確認、Agentからの質問は**World**画面に集約されます。

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

## Application設定

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

Cockpitは`http://127.0.0.1:8787/cockpit`で開きます。主なコマンドは次のとおりです。

```powershell
nexus-seed status
nexus-seed task "この依頼を分析して"
nexus-seed project "この依頼を直接Projectとして実行して"
nexus-seed config
```

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
nexus_seed/runtime/       durable event / continuation runtime
nexus_seed/processes/     Project routing、Resource観測のProcess handler
nexus_seed/providers/     A2A client、Project Agent transport
nexus_seed/cockpit/       Project / Knowledge中心のWeb UI
tests/                    unit、acceptance、restart convergence test
```

## 開発・確認

```powershell
pytest
python -m nexus_seed.app --once
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

- [アーキテクチャ詳細](docs/architecture.ja.md)
- [Architecture（英語）](docs/architecture.md)
- [開発時の不変条件と作業規約](AGENTS.md)
