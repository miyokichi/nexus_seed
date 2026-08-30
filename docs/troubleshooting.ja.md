# 困ったときの確認項目

まず次の3つを実行します。secretは表示されません。

```powershell
uv run python --version
uv run nexus-seed config
uv run nexus-seed status
```

## `No module named semantica`

原因は、Semantica extraを入れていない環境で実行していることです。

```powershell
uv sync --extra dev --extra semantica
uv run python -c "import semantica; print(semantica.__version__)"
```

`python`を直接実行せず、必ず`uv run python`を使います。

## Python 3.14が選ばれる

repository rootの`.python-version`は`3.13`です。

```powershell
Get-Content .python-version
uv sync --python 3.13 --extra dev --extra semantica
```

Semanticaの現在の依存packageにはWindows CPython 3.14向けwheelがありません。

## `modules/...`が空

`modules/`は親repositoryが直接管理しているため、通常のcloneで取得されます。
まずcheckoutの状態を確認します。

```powershell
Test-Path modules/knowledge/pyproject.toml
Test-Path modules/little_agent/pyproject.toml
git status --short
```

fileが存在しない場合はcloneまたはcheckoutが不完全です。作業中の変更を退避してから、
親repositoryを再取得してください。`git submodule` commandは使用しません。

## Cockpitへ接続できない

次を確認します。

1. `uv run nexus-seed`が別terminalで動いている
2. `.env`のhostとportが`127.0.0.1:8787`になっている
3. browserで`http://127.0.0.1:8787/cockpit`を開いている
4. tokenには`.env`の`NEXUS_SEED_WEBHOOK_TOKEN`を入力している

port使用状況:

```powershell
Get-NetTCPConnection -LocalPort 8787 -ErrorAction SilentlyContinue
```

## `task`がconnection refusedになる

`nexus-seed task`は常駐ApplicationのHTTP入口へ送信します。先に別terminalで起動します。

```powershell
uv run nexus-seed
```

serverを使わず1件を直接実行したい場合は`nexus-seed project`を使います。

## LLM確認が失敗する

```powershell
uv run nexus-seed --check-llm
```

確認項目:

- `NEXUS_SEED_LLM_ENABLED=true`
- providerが`anthropic`または`openai_compatible`
- model名がserverに存在する
- OpenAI互換URLが`/v1`まで含む
- `NEXUS_SEED_LLM_API_KEY_ENV`が指す環境変数に値がある
- local model serverが起動している

## A2A Agentへ接続できない

NEXUS SEED側:

```dotenv
NEXUS_SEED_PROJECT_AGENT_RUNTIME=a2a
NEXUS_SEED_PROJECT_AGENT_URL=http://127.0.0.1:8801
```

Agent Card確認:

```powershell
Invoke-WebRequest http://127.0.0.1:8801/.well-known/agent-card.json
```

失敗する場合はlittle_agentの`serve-a2a`を先に起動します。

## Resourceまたはworkspaceが拒否される

NEXUS SEEDとlittle_agentの両方で範囲を許可する必要があります。

```text
NEXUS readable root
  <= little_agent --readable-path

NEXUS project workspace root
  <= little_agent --writable-path
```

絶対pathを使い、実際にdirectoryが存在することを確認します。readableはwrite権限を含みません。

## Projectが人の回答待ちになる

これは失敗とは限りません。CockpitのOverviewまたはWorldのQuestionsを開き、回答します。

CLIの場合:

```powershell
uv run nexus-seed-knowledge pending --db "$HOME/.nexus_seed/nexus_seed.db"
uv run nexus-seed-knowledge answer --db "$HOME/.nexus_seed/nexus_seed.db" KNOWLEDGE_ID "回答内容"
```

## `--once`がすぐ終了しない

既存DBに未完了A2A assignmentがあると、remote Agentの状態確認を待つ場合があります。

```powershell
uv run nexus-seed status
```

Agent Runtimeが停止していれば再起動します。DBを削除して回避しないでください。未完了Projectと監査履歴が失われます。

## data directoryがrepository内だと言われる

安全のため、NEXUS SEEDはsource repository内をdata directoryにできません。

```dotenv
NEXUS_SEED_DATA_DIR=~/.nexus_seed
```

またはrepository外の絶対pathを指定します。

## 何もPlanningされない

次を確認します。

- LLM設定が必要な判断なのにLLMが無効ではないか
- ObservationまたはfileがKnowledgeへ記録されているか
- CockpitにProject ProposalやTask候補があり、人の承認待ちではないか
- Goalが「何を達成したいか」を明確に書いているか
- Semantica queryの場合、主要Entity名がqueryに含まれているか

## 最終確認

```powershell
uv run --extra dev --extra semantica pytest
git diff --check
```
