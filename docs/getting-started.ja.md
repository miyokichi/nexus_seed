# はじめてのNEXUS SEED

この文書は、repositoryを取得した直後の人が、NEXUS SEEDを起動して最初の依頼を送るまでの手順です。Windows PowerShellを基準にしています。

## できること

NEXUS SEEDは、自然文や許可されたファイルをKnowledgeとして記録し、必要なProjectを判断し、実行結果を再びKnowledgeへ戻します。

```text
人の依頼
  -> Observation
  -> Knowledge
  -> Planning
  -> Project
  -> Project Agent
  -> Result
  -> Knowledge / World View更新
```

NEXUS SEED自身はProjectの存在、状態、永続化、承認、Agentとの通信履歴を管理します。ファイル作成やTool利用などの実作業はProject Agentが担当します。

## 最短の起動手順

### 1. repositoryとsubmoduleを準備する

repository rootで実行します。

```powershell
git submodule update --init --recursive
```

`modules/knowledge`などが空の場合は、submoduleがまだ初期化されていません。上のcommandを再実行してください。

### 2. Python環境を作る

このprojectは`.python-version`でPython 3.13を選びます。対応範囲はPython 3.12または3.13です。`uv`に対象Pythonがなければ、`uv sync`がmanaged Pythonを取得します。

Semanticaも使える完全な開発環境を作る場合:

```powershell
uv sync --extra dev --extra semantica
```

確認:

```powershell
uv run python --version
uv run python -c "import semantica; print(semantica.__version__)"
```

commandは必ず`uv run ...`で実行します。virtual environmentを手動でactivateする必要はありません。

### 3. `.env`を作る

```powershell
Copy-Item .env.example .env
```

`.env`はGit管理対象外です。API keyやtokenを`.env.example`へ書かないでください。

最初に確認する項目:

```dotenv
NEXUS_SEED_DATA_DIR=~/.nexus_seed
NEXUS_SEED_WEBHOOK_HOST=127.0.0.1
NEXUS_SEED_WEBHOOK_PORT=8787
NEXUS_SEED_WEBHOOK_TOKEN=change-this-token
NEXUS_SEED_COCKPIT_ENABLED=true
NEXUS_SEED_KNOWLEDGE_LOOP_ENABLED=true
NEXUS_SEED_PROJECT_AGENT_RUNTIME=in_process
```

`NEXUS_SEED_DATA_DIR`にはDB、Resource、context、Project情報が保存されます。source repositoryの外を指定してください。空欄なら`~/.nexus_seed`です。

### 4. 設定を確認する

```powershell
uv run nexus-seed config
```

このcommandはAPI keyそのものを表示しません。次を確認します。

- LLMが有効か
- 使用するmodel名とURLが正しいか
- Project Agentが`in_process`か`a2a`か
- Resource rootが必要な範囲だけ許可されているか

### 5. NEXUS SEEDを起動する

```powershell
uv run nexus-seed
```

起動したterminalはそのままにします。停止するときは`Ctrl+C`です。

ブラウザで次を開きます。

```text
http://127.0.0.1:8787/cockpit
```

認証画面が表示されたら、`.env`の`NEXUS_SEED_WEBHOOK_TOKEN`を入力します。

## LLMを設定する

LLMを無効にしてもEvent、Knowledge、Project、Cockpit、永続化は動きます。ただし自然文の意味に基づく判断は制限されます。

### OpenAI互換server

Ollama、LM Studio、OpenAI互換proxyなどを使う例です。

```dotenv
NEXUS_SEED_LLM_ENABLED=true
NEXUS_SEED_LLM_PROVIDER=openai_compatible
NEXUS_SEED_LLM_BASE_URL=http://127.0.0.1:11434/v1
NEXUS_SEED_LLM_MODEL=使用するmodel名
NEXUS_SEED_LLM_API_KEY_ENV=OPENAI_API_KEY
OPENAI_API_KEY=local-server-key
```

`NEXUS_SEED_LLM_API_KEY_ENV`にはkeyではなく、keyを保持する環境変数名を書きます。local serverがkeyを検証しなくても、client側のためにdummy値が必要な場合があります。

### Anthropic

```dotenv
NEXUS_SEED_LLM_ENABLED=true
NEXUS_SEED_LLM_PROVIDER=anthropic
NEXUS_SEED_LLM_MODEL=使用するmodel名
NEXUS_SEED_LLM_API_KEY_ENV=ANTHROPIC_API_KEY
ANTHROPIC_API_KEY=実際のkey
```

接続確認:

```powershell
uv run nexus-seed --check-llm
```

ここで失敗する場合は、Applicationを起動する前にmodel名、URL、keyを直します。

## 最初の依頼を送る

### 常駐Applicationへ依頼する

1つ目のterminalで`uv run nexus-seed`を起動したまま、2つ目のterminalをrepository rootで開きます。

```powershell
uv run nexus-seed task "現在分かっていることを確認し、必要な作業を整理して"
```

`task`はIngressへ依頼を投入したらすぐ戻ります。進行状況はCockpitまたは次のcommandで確認します。

```powershell
uv run nexus-seed status
uv run nexus-seed status --json
```

同じ外部依頼の再送を重複させたくない場合:

```powershell
uv run nexus-seed task "月次レポートを確認して" --source-key monthly-report-2026-08
```

同じ`--source-key`の再送はIngressで重複排除されます。

### 1件のProjectを完了まで待つ

常駐serverとは別に、CLIで1件を直接Project Orchestratorへ渡し、完了まで待てます。

```powershell
uv run nexus-seed project "Project Alphaの残作業を確認して進めて"
```

状態だけ受け取り、待たずに戻る場合:

```powershell
uv run nexus-seed project "Project Alphaの残作業を確認して進めて" --no-wait
```

機械処理しやすい結果:

```powershell
uv run nexus-seed project "Project Alphaの残作業を確認して進めて" --json
```

## Cockpitの見方

| 画面 | 最初に見るもの |
| --- | --- |
| Overview | Runtime、LLM、進行中Project、判断待ち、error |
| World | Observation、World facts、Project提案、質問、成果物 |
| Activity | 最近配送されたEventとProcess |
| Projects | Project状態、Agent、A2A履歴、blocker |
| System | durable deliveryと失敗中Process |

人の判断が必要なときはOverviewの`Needs Attention`またはWorldを確認します。

- Project Proposal: 実行してよいか承認または拒否
- Question: Agentからの質問へ回答
- Artifact: 成果物を承認または差し戻し
- Completion Review: Project全体の完了を承認または差し戻し
- Unknown Entity: 同一Entityか別Entityかを確定

回答や承認はDBへ記録されます。browserを閉じても判断待ちは失われません。

## ファイルをKnowledgeへ入れる

Application起動時に次のdirectoryが作られます。

```text
NEXUS_SEED_DATA_DIR/resources
```

ここへplain text、Markdown、JSON、CSVなどの対応fileを置くと、file変更がIngressを通り、Resource、version、抽出結果、Knowledgeとして記録されます。

例:

```powershell
$resources = Join-Path $HOME ".nexus_seed/resources"
Copy-Item .\project_status.txt $resources
```

その後、内容を明示して依頼します。

```powershell
uv run nexus-seed task "project_status.txtを確認し、必要な成果物を判断して"
```

同じ内容のfileは重複versionになりません。`resources`以外の場所は自動では読みません。

## bootstrap contextを書く

初回起動後、次のfileが作られます。

```text
NEXUS_SEED_DATA_DIR/context/terms.md
NEXUS_SEED_DATA_DIR/context/goals.md
NEXUS_SEED_DATA_DIR/context/situation.md
```

- `terms.md`: 組織固有の言葉の意味
- `goals.md`: 良い状態、達成したいこと
- `situation.md`: 現在の状況や制約

人が書いた原文はそのまま保持されます。変更後は常駐loopが同期します。手動確認には次を使えます。

```powershell
uv run nexus-seed-knowledge context --db "$HOME/.nexus_seed/nexus_seed.db" --sync
```

## A2Aでlittle_agentを使う

`in_process`は確認用の制限されたAgentです。Toolでfileを作る実作業は、A2Aでlittle_agentなどの外部Agent Runtimeへ委譲します。

### 1. NEXUS SEED側のworkspaceを決める

Windowsの実際のuser名へ置き換えた絶対pathを使います。

```dotenv
NEXUS_SEED_PROJECT_AGENT_RUNTIME=a2a
NEXUS_SEED_PROJECT_AGENT_URL=http://127.0.0.1:8801
NEXUS_SEED_PROJECT_WORKSPACE=C:/Users/your-name/.nexus_seed/project-workspaces
NEXUS_SEED_PROJECT_RESOURCE_READ_ROOTS=C:/Users/your-name/.nexus_seed/resources
NEXUS_SEED_PROJECT_RESOURCE_WRITE_ROOTS=
```

workspaceを作成します。

```powershell
New-Item -ItemType Directory -Force "$HOME/.nexus_seed/project-workspaces"
```

### 2. little_agentを準備する

```powershell
Set-Location modules/little_agent
uv sync
Copy-Item .env.example .env
```

little_agentの`.env`には、little_agent自身が使うmodelを設定します。これはNEXUS SEEDの`NEXUS_SEED_LLM_*`とは別です。

```dotenv
OPENAI_BASE_URL=http://127.0.0.1:11434/v1
OPENAI_API_KEY=local-server-key
LITTLE_AGENT_MODEL=使用するmodel名
```

### 3. pathを限定してA2A serverを起動する

```powershell
uv run little-agent serve-a2a `
  --host 127.0.0.1 `
  --port 8801 `
  --auto-approve `
  --readable-path "$HOME/.nexus_seed/resources" `
  --writable-path "$HOME/.nexus_seed/project-workspaces"
```

`--auto-approve`はA2A serverに人が張り付いていないため必要です。信頼できるlocalhostで、readable/writable pathを限定した場合だけ使用してください。`--allow-any-path`は通常使用しません。

### 4. NEXUS SEEDを起動する

別terminalでrepository rootへ戻り、設定と接続先を確認して起動します。

```powershell
uv run nexus-seed config
uv run nexus-seed
```

little_agentを先に止めるとProjectはAgent待ちになります。再び同じportで起動すれば、NEXUS SEEDは永続Projectをreconcileします。

### 設定fileを混同しない

| file | 読むprocess | 用途 |
| --- | --- | --- |
| repository rootの`.env` | NEXUS SEED | NEXUS LLM、Project Agent接続先、data、Resource policy |
| `modules/little_agent/.env` | little_agent | little_agent自身のmodel、Tool、workspace、A2A server |
| rootの`a2a.json` | 現在の`nexus-seed` Applicationは読みません | 旧provider/skill binding設定。Project Agent接続先はroot `.env`を使用 |

NEXUS SEEDのmodelとlittle_agentのmodelは別設定です。片方の`.env`を変更しても、もう片方には反映されません。

## 停止と再起動

serverは`Ctrl+C`で停止します。DBを削除しない限り、同じ`NEXUS_SEED_DATA_DIR`で再起動すると未完了のEvent、Process、Projectを回収します。

HTTP serverを起動せず、回収可能な処理を一度だけ進める場合:

```powershell
uv run nexus-seed --once
```

未完了のA2A assignmentがある場合、`--once`はremote Agentの応答待ちになることがあります。先に`uv run nexus-seed status`とAgent Runtimeを確認してください。

## 次に読む

- [Semantica Knowledgeの使い方](semantica.ja.md)
- [困ったときの確認項目](troubleshooting.ja.md)
- [MVP Application Flow](mvp.md)
