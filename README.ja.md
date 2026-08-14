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

`AUTO`でも安全境界は省略しません。既存validator、限定Grant、ActionProposal、検証、
Activation、Work reconciliationをすべて通ります。Runtime/Core/Policy変更や
unrestricted shell/networkは引き続き禁止です。

## はじめかた

Python 3.12以上が必要です。

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e ".[dev]"
```

再起動を含むCoreデモを実行します。

```bash
python -m nexus_seed.demo
```

全テストを実行します。

```bash
pytest
```

## LLM接続

標準の`.env.example`はOpenAI API互換のローカルサーバー向けです。URLと実際に
ロードしたモデルIDを指定して有効化します。

```dotenv
NEXUS_SEED_LLM_ENABLED=true
NEXUS_SEED_LLM_PROVIDER=openai_compatible
NEXUS_SEED_LLM_BASE_URL=http://127.0.0.1:1234/v1
NEXUS_SEED_LLM_MODEL=your-loaded-model
```

LM Studioは通常`1234`、Ollamaは通常`11434`ポートです。ローカルサーバー側が
要求しない限り、OpenAI SDKもAPIキーも必要ありません。

Runtime生成後、アプリケーションの初期化時に1回呼び出します。

```python
from nexus_seed.llm_config import configure_llm

configure_llm(runtime)
```

これで意味解釈、Plan選択、Capability獲得提案、sandbox内の実装生成が同じ
Backendへ接続されます。未接続時は決定論的フォールバックで動作します。
Runtimeを再生成した場合は再度呼び出してください。Anthropic接続も
`NEXUS_SEED_LLM_PROVIDER=anthropic`と任意SDKによって引き続き利用できます。

## ディレクトリ構成

```text
nexus_seed/core/          固定データモデル
nexus_seed/runtime/       routing、scheduling、execution、recovery
nexus_seed/storage/       SQLite永続化
nexus_seed/processes/     Process定義とhandler
nexus_seed/autonomy/      Phase 5DのSession、Policy、Budget、Trace
tests/                    受入テストと再起動収束テスト
```

## 詳細資料

- [詳細アーキテクチャとPhase履歴](docs/architecture.ja.md)
- [Detailed architecture (English)](docs/architecture.md)
- [開発時に守るInvariant](AGENTS.md)

現在の実装範囲は**Phase 5Dまで**です。Phase 6には進んでいません。
