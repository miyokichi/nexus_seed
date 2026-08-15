# NEXUS SEED

NEXUS SEEDは、観測・判断・中断・再開・外部操作・Capability拡張を安全に実行する、
永続的なイベント駆動ランタイムです。

中心にある仕組みはシンプルです。

```text
Event -> Process -> State -> Continuation -> Event -> Resume
```

固定プリミティブは`Event`、`Process`、`State`、`Context`、`Continuation`、`Runtime`
の6つだけです。Skill、Agent、Workflow、Observerなどは、新しい基底型ではなく
Processが担う役割として表現します。

*[English](README.md)*

## 実装済みの機能

- SQLiteによる原子的な状態遷移、retry、timer、crash recovery、再起動可能なContinuation
- 履歴と出自を追跡できるWorld State
- CapabilityベースのWork matching、複数ProcessのPlan、有界replanning
- LLM入力と外部Actionのvalidation・policy・監査境界
- 永続Ingress、Resource versioning、抽出、Event配送
- Sandbox内Capability構築、検証、人手確認付きProduction activation、rollback
- `AUTO / REVIEW_REQUIRED / FORBIDDEN`とBudgetを備えたPhase 5D自律Capability取得
- 内部Process、Directory Skill、外部Agentを統合するPhase 5E Provider Federation

`AUTO`でも安全境界は省略しません。既存validator、限定Grant、ActionProposal、検証、
Activation、Work reconciliationをすべて通ります。Runtime/Core/Policy変更や
unrestricted shell/networkは引き続き禁止です。

## 実際に動かす

NEXUS SEEDはチャット画面ではなく、外部Eventを受け取ってProcess群を動かす
常駐サーバーです。通常は次のように起動し、別のターミナルや外部システムから
WebhookへEventを送ります。

### 1. インストール

Python 3.12以上が必要です。PowerShellでリポジトリ直下から実行します。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

### 2. `.env`を設定

初回は`.env.example`をコピーします。このリポジトリにはローカル用`.env`も
作成済みです。

```powershell
Copy-Item .env.example .env
```

最低限確認する設定は次のとおりです。永続データは安全境界のためソースツリー外へ
置きます。

```dotenv
NEXUS_SEED_DATA_DIR=C:/Users/user/AppData/Local/nexus-seed
NEXUS_SEED_WEBHOOK_HOST=127.0.0.1
NEXUS_SEED_WEBHOOK_PORT=8787
NEXUS_SEED_WEBHOOK_TOKEN=十分に長い任意の文字列
```

OpenAI API互換のローカルLLMを使う場合は次も設定します。

```dotenv
NEXUS_SEED_LLM_ENABLED=true
NEXUS_SEED_LLM_PROVIDER=openai_compatible
NEXUS_SEED_LLM_BASE_URL=http://127.0.0.1:1234/v1
NEXUS_SEED_LLM_MODEL=サーバーが返す正確なモデルID
```

LM Studioは通常`1234`、Ollamaは通常`11434`ポートです。APIキーを要求しない
ローカルサーバーなら`OPENAI_API_KEY`は空で構いません。モデル一覧は次のように
確認できます。

```powershell
Invoke-RestMethod http://127.0.0.1:1234/v1/models
```

LLMサーバーをまだ起動しない場合は`NEXUS_SEED_LLM_ENABLED=false`にします。
その場合も決定論的フォールバックでRuntimeは動作します。

### 3. 設定・DB・復旧処理を確認

HTTPサーバーを起動せず、全Phaseを登録して未処理Eventを一度drainします。

```powershell
nexus-seed --once
```

JSONで`status: idle`、DBパス、LLM有効状態などが表示されれば準備完了です。
続いて、設定したLLMへ実際に1回問い合わせ、JSON応答まで確認します。

```powershell
nexus-seed --check-llm
```

`success: true`と`response: {"status": "ok"}`が返ればLLM接続も完了です。

### 4. 常駐サーバーを起動

```powershell
nexus-seed
```

起動時にWebhook URL、DBパス、LLM接続設定が表示されます。Phase 1〜5EのProcess、
durable delivery、retry/timer、Capability acquisitionがすべて登録され、1秒ごとに
Runtimeがtickします。終了は`Ctrl+C`です。同じコマンドで再起動するとSQLiteから
未完了処理を復旧します。

### 5. Eventを投入

サーバーを起動したまま、別のPowerShellから送信します。

```powershell
$headers = @{ "X-Ingress-Token" = "上で設定したtoken" }
$body = @{
    source_event_key = "manual-20260815-001"
    event_type = "human_message"
    payload = @{ text = "顧客レポートを解析し、必要な作業を判断してください" }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
    -Uri http://127.0.0.1:8787/ingress/webhook `
    -Method Post `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body
```

`202 Accepted`はEventがSQLiteへ安全に保存されたことを表します。Process処理や
LLM呼び出しの完了を待った応答ではありません。同じ`source_event_key`を再送すると
重複処理せず`duplicate: true`を返します。

### 6. 作成されるデータ

`NEXUS_SEED_DATA_DIR`の下に次が作成されます。

```text
nexus_seed.db          Event・Process・監査履歴を持つSQLite DB
resources/             読み書きを許可したResource領域
actions/               LocalFileActionBackendの出力先
construction/          生成コードの隔離workspace
installed_extensions/  検証・承認後の有効化先
```

`construction`と`installed_extensions`はソースコード領域から分離されます。生成物が
いきなり本体を書き換えることはありません。

## 開発・確認

Coreだけの再起動デモと全テストは次で実行できますが、通常運転には不要です。

```powershell
python -m nexus_seed.demo
pytest
```

## ディレクトリ構成

```text
nexus_seed/core/          固定データモデル
nexus_seed/runtime/       routing、scheduling、execution、recovery
nexus_seed/storage/       SQLite永続化
nexus_seed/processes/     Process定義とhandler
nexus_seed/autonomy/      Phase 5DのSession、Policy、Budget、Trace
nexus_seed/providers/     Phase 5EのProvider、委譲、Skill import、Trace
tests/                    受入テストと再起動収束テスト
```

## 詳細資料

- [詳細アーキテクチャとPhase履歴](docs/architecture.ja.md)
- [Detailed architecture (English)](docs/architecture.md)
- [開発時に守るInvariant](AGENTS.md)

現在の実装範囲は**Phase 5Eまで**です。Phase 6には進んでいません。
