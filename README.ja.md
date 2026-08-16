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

設計をPhase順ではなく一枚の流れで把握する場合は、
**[NEXUS SEED 全体像](docs/system-overview.ja.md)**を先に読んでください。

## 実装済みの機能

- SQLiteによる原子的な状態遷移、retry、timer、crash recovery、再起動可能なContinuation
- 履歴と出自を追跡できるWorld State
- CapabilityベースのWork matching、複数ProcessのPlan、有界replanning
- LLM入力と外部Actionのvalidation・policy・監査境界
- 永続Ingress、Resource versioning、抽出、Event配送
- Capability gap分析、Sandbox内構築、検証、人手確認付きProduction activation、rollback
- `AUTO / REVIEW_REQUIRED / FORBIDDEN`とBudgetを備えたPhase 5D自律Capability取得
- 内部Process、Directory Skill、外部Agentを統合するPhase 5E Provider Federation
- 認証・認可された明示Command、永続Goal、Work制御、監査履歴を備えたPhase 5G Control Plane
- feature flagで無効化できるPhase 6 Self/Master projection、永続Intention、Attention、Experience/Reflection、自発活動
- Overview、Being、因果Activity、Work、Review、Provider、Systemを表示する認証付きHuman Cockpit
- Goal作成でProjectが立ち上がり、所属Work・status・situationを既存recordから導出するGoal中心のProject lifecycle
- Project Situationだけを根拠にProjectの状況を自然言語で説明するread-only Project Chat

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
NEXUS_SEED_COCKPIT_ENABLED=true
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

ブラウザで`http://127.0.0.1:8787/cockpit`を開きます。データ取得時にWebhookと同じTokenを
入力します。Tokenはブラウザのtab単位session storageだけに保持されます。Cockpitは既存の
projectionとtraceを読み、操作はすべてPhase 5Gの`/control`へ送ります。表示は自動更新
されません。最新状態の取得は右上の更新ボタンで明示的に行います。
`NEXUS_SEED_COCKPIT_ENABLED=false`にするとCockpit routeだけを無効化でき、Runtime、Webhook、
CLIの挙動は変わりません。

Projectは「1つのGoal + そのGoalのためのWork集合」です。Workが1件でもProjectとして
成立します。Projectを先に作る操作はなく、Goalを作れば同時に立ち上がります。

```powershell
nexus-seed control '/goal create title="Runtime health" objective="RuntimeとLLMの状態を把握する" priority=HIGH'
```

CommandはGoal idから導出した`project_id`を返し、そのProjectはCockpitのProjects一覧へ
すぐ表示されます。保存するのは関連付けだけで、title / objective / lifecycleはroot Goalが
持ち続けます。したがって`/goal pause` / `/goal resume` / `/goal cancel`がそのまま
Project lifecycleです。Goalから生成されたWorkは同じProjectへ所属し、replanやrestart後も
同じProjectへ収束します。Project statusは
`CANCELLED > PAUSED > BLOCKED > NEEDS_ATTENTION > ACTIVE > PLANNING > COMPLETED > IDLE`
の固定順で導出するため、同じ状態からは常に同じ結果になります。

明示指定も従来どおり使えます（Workの`project=project-a`、Goalの
`metadata={"project_id":"project-a", ...}`）。Control Planeを通さずに保存されたGoalは、
読み取り時にProjectを与えず未所属のままにします。

CockpitのProjects画面ではProjectを開き、Project Situationの横でNEXUS SEEDに質問できます。
同じ内容はHTTPからも参照できます。

```text
GET  /projects
GET  /projects/project-a/situation
GET  /projects/project-a/chat
POST /projects/project-a/chat   {"message": "今このプロジェクトは何で止まってる？"}
```

Project Chatの回答は、そのProjectのProject Situation projection、同じProjectのThread、
今回の質問だけから生成します。SQLiteの直接探索も、他Projectの参照も行いません。
このPhaseは説明専用です。「キャンセルして」「優先して」「この方針で進めて」のような
状態変更要求は実行せず`READ_ONLY_REFUSED`として断ります。変更は従来どおり`/control`から
実行してください。別Projectについての質問は`OUT_OF_SCOPE`として断り、勝手に検索しません。

Thread履歴は`project_chat_threads` / `project_chat_messages`に保存され、再起動後も復元
されます。Chat履歴は会話であり確認済みWorld Stateではないため、Observation、StateDelta、
World Stateにはなりません。LLM未接続時やLLMの出力が不正な場合は、projectionの確定事実
だけを`LLM_UNAVAILABLE` / `LLM_FAILED` / `LLM_INVALID`と明示して返し、Runtimeには影響
しません。

### 5. タスクを投入

サーバーを起動したまま、別のPowerShellから実行します。`.env`のURLとTokenは
コマンドが自動的に使用します。

```powershell
nexus-seed task "顧客レポートを解析し、必要な作業を判断してください"
```

これは`human_message` EventをIngressへ投入します。LLMが有効なら通常の
`interpret_event_llm` Processが処理し、Proposalの検証とPolicy判定を通ります。
応答の`status: accepted`はEventがSQLiteへ安全に保存されたという意味で、Processや
LLM処理の完了を待った結果ではありません。

再送時の重複を確実に防ぎたい場合は、外部側で一意なキーを指定します。

```powershell
nexus-seed task "同じ依頼" --source-key crm-ticket-12345
```

任意のEventも投入できます。複雑なJSONはファイルにするとPowerShellのquoteを
気にせず扱えます。

```powershell
nexus-seed event process_parameter_changed `
    --payload '{"entity":"reactor-1","attribute":"target","value":42}' `
    --source-key sensor-change-001

nexus-seed event measurement_completed --payload-file measurement.json
```

### 6. 状況とレビューを確認

`status`はSQLiteを読み取り専用で開き、Event数、Work/Process/Deliveryの状態、最近の
処理を表示します。常駐サーバーを止める必要はありません。

```powershell
nexus-seed status
nexus-seed status --json
```

人の判断待ちになった処理は次で確認・再開できます。レビューの種類はContinuation
から自動判別されるため、Event名を指定する必要はありません。

```powershell
nexus-seed reviews
nexus-seed review <reviewsに表示されたID> approve
nexus-seed review <reviewsに表示されたID> reject
```

`modify`が対応するレビューでは追加JSONも渡せます。

```powershell
nexus-seed review <ID> modify --payload-file replacement.json
```

`status`はDB内の永続状態を示すコマンドであり、HTTPサーバーの死活監視では
ありません。`task`や`event`が接続できない場合は、`nexus-seed`が別ターミナルで
起動しているかを確認してください。

### 7. 明示CommandとGoalで制御

Phase 5GのControl Planeは、自然言語の`task` Eventと明示Commandを分離します。
明示CommandはLLMを通らず、Schema ValidationとHumanIdentityのPermission確認後に
実行されます。常駐サーバーを起動した別ターミナルから使います。

```powershell
# Control Console summary
nexus-seed control '/status'

# 明示的なWork作成
nexus-seed control '/task create objective="Project Aの最新測定結果を解析" priority=HIGH cloud_forbidden=true'

# 結果に表示されたWork IDを操作
nexus-seed control '/work <WORK-ID>'
nexus-seed control '/pause <WORK-ID>'
nexus-seed control '/resume <WORK-ID>'
nexus-seed control '/priority <WORK-ID> CRITICAL'
nexus-seed control '/deadline <WORK-ID> 2026-08-20T18:00+09:00'
nexus-seed control '/provider <WORK-ID> PREFER local_runtime'
nexus-seed control '/trace <WORK-ID>'
nexus-seed control '/cancel <WORK-ID>'
```

レビューは既存のContinuation/Event境界へ変換され、新しい承認経路を作りません。

```powershell
nexus-seed control '/approve <REVIEW-ID>'
nexus-seed control '/reject <REVIEW-ID>'
```

長期GoalはWorkRequirementとは別に保存され、通常の`evaluate_goal` Processが現在の
World Stateと既存Workから不足Workを生成します。同じ不足は再評価・再起動後も
deterministic keyで1件へ収束します。

```powershell
nexus-seed control '/goal create title="Project A review readiness" objective="Project Aをレビュー可能状態へする" priority=HIGH deadline=2026-09-15'
nexus-seed control '/goals'
nexus-seed control '/goal show <GOAL-ID>'
nexus-seed control '/goal evaluate <GOAL-ID>'
nexus-seed control '/goal pause <GOAL-ID>'
nexus-seed control '/goal resume <GOAL-ID>'
nexus-seed control '/goal cancel <GOAL-ID>'
```

`NEXUS_SEED_CONTROL_IDENTITY`と`NEXUS_SEED_CONTROL_PERMISSIONS`は`.env`で設定します。
HTTPのBearer/Ingress Tokenは接続認証、HumanIdentity PermissionはCommand認可であり、
役割が異なります。Commandは既存Permission・Action・Validator・Autonomy Policyを
緩和できません。

Phase 6は既定で有効です。Phase 5G互換へ戻す場合だけ`.env`へ次を追加します。

```dotenv
NEXUS_SEED_PHASE6_ENABLED=false
```

無効時はPhase 6 Processもwake Eventも追加されず、Phase 5Gと同じ挙動です。有効時も
常時busy loopは作らず、active Goal、未解決Intention、未回答のSelf questionがある
起動時だけ`existence_wakeup`を追加し、有限のProcess連鎖が終われば通常のEvent待ちへ戻ります。
success criteria未指定のGoalは、Intention確立後に具体的Workへ構造化分解されます。
`advance_human_goal`のような内部fallbackをCapability Acquisitionへ渡すことはありません。

### 8. 作成されるデータ

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
nexus_seed/extension/     Phase 5AのCapability gapと取得提案
nexus_seed/construction/  Phase 5BのSandbox内構築と検証
nexus_seed/installation/  Phase 5Cの承認付きActivationとrollback
nexus_seed/autonomy/      Phase 5DのSession、Policy、Budget、Trace
nexus_seed/providers/     Phase 5EのProvider、委譲、Skill import、Trace
nexus_seed/control/       Phase 5GのCommand、Identity、Goal、認可
nexus_seed/presence/      Phase 6のSelf/Master/Intention projectionとExperience trace
nexus_seed/projects/      read-onlyなProject Situationのmodelとprojection
nexus_seed/chat/          read-onlyなProject Chatのcontext、guard、回答生成
nexus_seed/cockpit/       人間向けread modelと依存なしWeb UI
tests/                    受入テストと再起動収束テスト
```

## 詳細資料

- [詳細アーキテクチャとPhase履歴](docs/architecture.ja.md)
- [Detailed architecture (English)](docs/architecture.md)
- [開発時に守るInvariant](AGENTS.md)

現在の実装範囲には既定ON・Phase 5G互換flag付きの**Phase 6 — Persistent Being**を含みます。Phase 7には進んでいません。
