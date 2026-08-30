# NEXUS SEED

NEXUS SEEDは、出典付きKnowledge Runtimeを備えた永続Project Orchestratorです。
許可された入力を観測し、知識として記録し、必要なProjectを判断して1つのProject Agentへ
委譲し、その結果を再びKnowledgeへ戻します。

```text
Observation -> Knowledge -> Planning -> Project -> Agent/A2A
     ^                                            |
     +--------------- Result ---------------------+
```

Projectの識別子、ライフサイクル、永続化、review、監査可能なAgent通信はNEXUS SEEDが
管理します。Task分解、Tool選択、実作業はProject Agentが管理します。

*[English](README.md)*

## 最初に読む

初めて使う場合は、[はじめてのNEXUS SEED](docs/getting-started.ja.md)を上から順に進めてください。インストール、`.env`、LLM、最初の依頼、Cockpit、little_agentの起動まで説明しています。

| 目的 | 構成 |
| --- | --- |
| DB、Cockpit、基本経路だけ確認 | LLM無効 + `in_process` |
| 自然文を判断させる | NEXUS LLM有効 + `in_process` |
| file作成などの実作業を行う | NEXUS LLM有効 + A2A little_agent |
| 業務概念をsemantic検索する | 上記 + Semantica snapshot |

関連資料:

- [ドキュメント索引](docs/README.ja.md)
- [Semantica Knowledgeの使い方](docs/semantica.ja.md)
- [困ったときの確認項目](docs/troubleshooting.ja.md)

## クイックスタート

### 必要環境

- Python 3.12または3.13（project既定は3.13）
- [uv](https://docs.astral.sh/uv/)（推奨）
- 4つのNEXUS moduleと`little_agent` submodule

Semanticaの現在の依存先`gensim`にはWindows CPython 3.14向けwheelがないため、
Python 3.14は対象外です。

```powershell
git submodule update --init --recursive
uv sync --extra dev --extra semantica
Copy-Item .env.example .env
```

macOS/Linuxでは最後の行を`cp .env.example .env`に置き換えます。

data directoryの既定値は`~/.nexus_seed`です。`NEXUS_SEED_DATA_DIR`を設定する場合は、
source repositoryの外を指定してください。

secretを表示せず、解決後の設定を確認できます。

```powershell
uv run nexus-seed config
```

LLMを有効にした場合は、Application起動前に接続を確認します。

```powershell
uv run nexus-seed --check-llm
```

Applicationを起動します。

```powershell
uv run nexus-seed
```

`http://127.0.0.1:8787/cockpit`を開きます。tokenを求められた場合は、`.env`の
`NEXUS_SEED_WEBHOOK_TOKEN`を入力してください。終了は`Ctrl+C`です。

別terminalから最初の依頼を送ります。

```powershell
uv run nexus-seed task "現在分かっていることを確認し、必要な作業を整理して"
uv run nexus-seed status
```

`task`は常駐Applicationへ非同期に投入します。serverを使わず1件を完了まで待つ場合は、
`uv run nexus-seed project "依頼内容"`を使います。

HTTP serverを起動せず、回収可能なdurable workを一度だけ進める場合:

```powershell
uv run nexus-seed --once
```

## 基本操作

通常運転では`nexus-seed`を1つ起動したままにし、別terminalから依頼や確認を行います。

```powershell
uv run nexus-seed task "現在の状況から必要な作業を判断して"
uv run nexus-seed status
uv run nexus-seed project "この依頼を実行して結果まで待って"
```

| command | 用途 |
| --- | --- |
| `nexus-seed` / `nexus-seed serve` | durable applicationとCockpitを起動 |
| `nexus-seed task TEXT` | Ingress経由で非同期に依頼を投入 |
| `nexus-seed project TEXT` | 依頼をroutingし、既定ではProject完了まで待機 |
| `nexus-seed status` | delivery、Process、Projectの状態を表示 |
| `nexus-seed config` | secretを伏せて解決済み設定を表示 |
| `nexus-seed --check-llm` | 設定した推論modelを1回だけ確認 |

`.env`以外を使用する場合は`--env-file PATH`を指定します。

詳しい操作例、fileの入れ方、A2A little_agentの起動方法は
[はじめてのNEXUS SEED](docs/getting-started.ja.md)を参照してください。

## 動作するループ

### 永続Application loop

常駐ApplicationはEvent、Process状態、Knowledge、Resource、Project、A2A message、
checkpoint、review判断をSQLiteへ保存します。同じdata directoryで再起動すると、未完了の
処理を再開します。

```text
Ingress -> durable Event delivery -> Knowledge更新 -> Project routing
        -> Project Agent -> result/review -> Knowledge更新
```

外部入力はIngressを通り、外部側の識別子で重複排除されます。fileを自動観測する範囲は
`NEXUS_SEED_DATA_DIR/resources`だけです。それ以外は、設定済みrootの中からProject単位で
明示的に許可したResourceだけをAgentへ渡します。

### 有限closed loop

`nexus_seed.app.flows`には、用途の異なる2つのApplication APIがあります。

- `run_once(request)`はObserverからresultまでを正確に1回だけ実行します。
- `run_until_stable(request, max_iterations=3)`は、明示的な`world_facts`によって
  Knowledge World Viewが変わった場合だけ再Planningします。

`run_until_stable`は`no_action`、`stable_world`、`blocked`、`repeated_state`、
`max_iterations`のいずれかで停止します。daemonでも無制限retryでもありません。

```python
report = await application.run_until_stable(request, max_iterations=3)
print(report.stop_reason, report.project_ids)
```

raw execution resultはそのままKnowledge履歴へ残ります。World Viewを更新するのは、
次のように明示された構造化factだけです。

```json
{
  "world_facts": [
    {"entity": "performance_report", "attribute": "status", "value": "created"}
  ]
}
```

### SemanticaによるSemantic Knowledge

Canonical YAML v0.1をsemantic Knowledgeの資料境界として使用します。業務概念はEntity、
数値や単位などの値はPropertyとして保持し、資料が明示したRelationとSemanticaの推論を
分離します。任意extraを導入し、同梱sampleを投入してqueryできます。

```powershell
uv sync --extra dev --extra semantica
uv run nexus-seed-semantica `
  --snapshot "$HOME/.nexus_seed/semantica-knowledge.json" `
  --ontology modules/knowledge/samples/ontology_v0.1.yaml `
  ingest modules/knowledge/samples/assumption_v0.1.yaml
uv run nexus-seed-semantica `
  --snapshot "$HOME/.nexus_seed/semantica-knowledge.json" `
  query "GenX WL Width"
```

snapshotはfileへ永続化され、追加serverなしで再起動後もqueryできます。`.env`の
`NEXUS_SEED_SEMANTICA_SNAPSHOT`と`NEXUS_SEED_SEMANTICA_ONTOLOGY`へ設定すれば、
CLI optionを省略できます。会社では資料からCanonical YAMLを作るconverter、YAML本体、
小さなOntologyだけを差し替え、`nexus_knowledge`以降のPlanning経路は変更しません。

Canonical YAMLの全項目、Entity/Propertyの分け方、Ontology、会社資料への置換手順は
[Semantica Knowledgeの使い方](docs/semantica.ja.md)にあります。

## 設定

Application設定の正本は`.env.example`です。`.env`と他の`.env.*`はGit管理対象外で、
`.env.example`だけを追跡します。

### Application

| 変数 | 既定値 | 説明 |
| --- | --- | --- |
| `NEXUS_SEED_DATA_DIR` | `~/.nexus_seed` | source repository外を指定 |
| `NEXUS_SEED_WEBHOOK_HOST` | `127.0.0.1` | localhost外ではtoken必須 |
| `NEXUS_SEED_WEBHOOK_PORT` | `8787` | webhookとCockpitのport |
| `NEXUS_SEED_WEBHOOK_TOKEN` | 空 | localhostでも設定推奨、外部listenでは必須 |
| `NEXUS_SEED_TICK_SECONDS` | `1` | Runtimeのpoll間隔 |
| `NEXUS_SEED_LOG_LEVEL` | `INFO` | `DEBUG`から`CRITICAL`まで |
| `NEXUS_SEED_COCKPIT_ENABLED` | `true` | `/cockpit`を有効化 |
| `NEXUS_SEED_KNOWLEDGE_LOOP_ENABLED` | `true` | 永続Knowledge loopを有効化 |
| `NEXUS_SEED_KNOWLEDGE_POLL_SECONDS` | `60` | Knowledge/Resourceのpoll間隔 |

### NEXUS SEED自身の推論model

`NEXUS_SEED_LLM_*`はrouting、状況評価、質問応答に使うNEXUS SEED自身のmodel設定です。
外部Project Agentが使うmodelの設定ではありません。

LLMは任意です。`NEXUS_SEED_LLM_ENABLED=false`でも永続化、Ingress、Knowledge記録、
Project、Cockpitは動作しますが、意味に基づく判断は制限されます。

OpenAI互換local serverの例:

```dotenv
NEXUS_SEED_LLM_ENABLED=true
NEXUS_SEED_LLM_PROVIDER=openai_compatible
NEXUS_SEED_LLM_BASE_URL=http://127.0.0.1:1234/v1
NEXUS_SEED_LLM_MODEL=local-model
NEXUS_SEED_LLM_API_KEY_ENV=OPENAI_API_KEY
OPENAI_API_KEY=local-server-key
```

`NEXUS_SEED_LLM_API_KEY_ENV`にはkeyそのものではなく、keyを保持する環境変数名を
設定します。

### Project Agent

`NEXUS_SEED_PROJECT_AGENT_RUNTIME`でProjectの実行先を選びます。

- `in_process`: localで動く制限付きの決定的runtime。開発・確認向け。
- `a2a`: Project全体を外部Agent Runtimeへ委譲。

`a2a`では`NEXUS_SEED_PROJECT_AGENT_URL`が必須です。外部Agentは自身のmodelとSkillsを
管理します。NEXUS SEEDが送るのはProject goalと許可済みResourceであり、実装方法では
ありません。

### Resource権限

host accessは既定で閉じています。Projectへ許可できるrootを設定します。

```dotenv
NEXUS_SEED_PROJECT_RESOURCE_READ_ROOTS=C:/work/shared;C:/work/specs
NEXUS_SEED_PROJECT_RESOURCE_WRITE_ROOTS=C:/work/shared/out
```

区切りはWindowsでは`;`、Unixでは`:`です。write rootはread可能範囲にも含めてください。
read grantは原本を参照し、write対象はProject workspaceへstageされ、成果を採用したときだけ
回収されます。

```powershell
uv run nexus-seed-knowledge grants --db path/to/nexus_seed.db
uv run nexus-seed-knowledge grant --db path/to/nexus_seed.db PROJECT_ID `
  "file:C:/work/shared/input.csv" --reason "Projectが要求した入力"
uv run nexus-seed-knowledge collect --db path/to/nexus_seed.db PROJECT_ID
```

## Cockpit

| 画面 | 用途 |
| --- | --- |
| **Overview** | Runtime、Project、review待ち、errorの概要 |
| **World** | 観測元、World facts、提案、質問、Entity、成果物 |
| **Activity** | 最近のEvent・Process・Project活動 |
| **Projects** | Project状態、Agent、blocker、A2A履歴、追加指示 |
| **System** | durable deliveryとProcess failureの診断 |

PC情報は自動登録されません。**World → Observation Sources**で観測元を作成し、OS、
hostname、CPU、memory、`NEXUS_SEED_DATA_DIR`のdisk使用量から必要な固定項目だけを
選択します。process一覧、任意のfile、user directoryを暗黙に走査しません。

## 設計境界

固定primitiveは`Event`、`Process`、`State`、`Context`、`Continuation`、`Runtime`の
6つです。Project、Agent、Knowledge、Resource、Work、Capabilityはdomain recordまたは
Processの役割であり、新しいcore primitiveではありません。

```text
Knowledge Runtime       世界について何を知っているか
Project Orchestrator    どの仕事を存在させ、どの状態で管理するか
Project Agent           1つのProjectをどう実行するか
Runtime                 durable executionをどう配送・再開するか
```

独立moduleはNEXUS SEEDや互いをimportせず、NEXUS SEEDが安定contractを通じてcomposeします。

```text
modules/knowledge
modules/observer
modules/planner
modules/project_manager
modules/little_agent       A2A越しの外部Agent
```

## 開発

```powershell
uv run --extra dev pytest
uv run --extra dev pytest tests/test_module_boundaries.py
uv run --extra dev python -m nexus_seed.app --once
```

testは一時SQLite DBを使います。外部Agentが必要なintegration testは`tests/integration/`に
あり、endpoint未設定時はskipされます。

```text
nexus_seed/app/             Application compositionとCLI
nexus_seed/core/            固定された6 primitive
nexus_seed/runtime/         durable routing、execution、resume、delivery
nexus_seed/platform/        module間contract
nexus_seed/resources/       Resource/Version/Representation基盤
modules/                    独立module repository
tests/                      unit、boundary、restart、E2E test
```

## 詳細資料

- [ドキュメント索引](docs/README.ja.md)
- [はじめてのNEXUS SEED](docs/getting-started.ja.md)
- [Semantica Knowledgeの使い方](docs/semantica.ja.md)
- [困ったときの確認項目](docs/troubleshooting.ja.md)
- [MVP Application Flow](docs/mvp.md)
- [開発上の不変条件](AGENTS.md)
