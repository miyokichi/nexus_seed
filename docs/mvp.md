# NEXUS SEED MVP layer

`nexus_seed.mvp` は、既存システムを置き換えずに次の一周だけを明示的に接続する薄い Application 層です。

```text
External/Human input -> Observer -> Knowledge -> Planner -> Human approval
                     -> Project Manager -> Executor -> Result -> Knowledge
```

各境界は `nexus_seed.mvp.interfaces` の Protocol で交換できます。MVP Runtime は呼び出し順だけを持ち、Project を提案するか、どう実行するか、承認するかを判断しません。

## 既存コードの調査分類

| 分類 | 既存資産 | MVP での扱い |
|---|---|---|
| A: そのまま再利用 | `Runtime.ingress`, `ManualAdapter`, `Database`, `KnowledgeStore`, `KnowledgeLedger`, `ProjectStore`, 既存 LLM `ExecutionBackend` | 外部入力境界・永続化・追記型 Knowledge・Project record・推論 backend として利用 |
| B: Adapter で再利用 | 既存 Ingress、`orchestrator.ProjectManager`, async `ExecutionBackend` | `ManualIngressObserver`, `ExistingProjectManagerAdapter`, `ExistingBackendLLMProvider` で MVP Interface へ変換 |
| C: MVP では不要 | 複数 Agent routing、A2A、Cockpit、高度な Knowledge consolidation | MVP loop には接続しない |
| D: Full 版では必要だが MVP では使わない | Process resume、Resource watcher、Context compiler、Project Agent orchestration | 既存実装を保持し、MVP Runtime から独立 |

CLI の外部入力は `ManualIngressObserver` により、既存 Ingress の検証・dedup・Event 永続化を通過してから `Observation` になります。Observer は Event を配送せず、既存 Process / Runtime の実行はホスト側の別責務です。`TextObserver` は、埋め込み先がすでに認可・dedup 済みの文字列を渡す場合と単体テスト用です。Observer は入力を解釈しません。Knowledge Gateway の検索は既存 Ledger の current HEAD に対する決定的な文字列検索だけで、Vector DB や Ontology は追加していません。Executor の最小 Tool は、明示された workspace の `README.md` を必要時に読むだけで、ファイル変更やコマンド実行は行いません。

## 実行

既存の `.env` で LLM を有効にして実行します。

```bash
uv run nexus-seed-mvp "NEXUS SEED の README を改善する。" --workspace .
```

ネットワークなしで骨格だけを確認する場合は、明示的な demo/test 用 provider を使えます。

```bash
uv run nexus-seed-mvp "NEXUS SEED の README を改善する。" \
  --workspace . --yes --mock-llm --json
```

`--yes` を付けない限り、Project 作成前に CLI で承認を求めます。永続データは `--data-dir`（既定は `NEXUS_SEED_DATA_DIR` または `~/.nexus_seed`）の既存 `nexus_seed.db` に保存されます。

## テスト

```bash
uv run --extra dev pytest -q tests/test_mvp_modules.py tests/test_mvp_runtime.py
```

`nexus_seed.mvp.testing` に Observer、Knowledge Gateway、Planner、Project Manager、Executor、LLM Provider の全 test double があり、E2E テストは同じ Runtime に差し替え可能であることを確認します。

## Frozen architecture

MVP の一周と全回帰テストの合格をもって、この公開骨格を凍結します。

```text
NEXUS SEED MVP Architecture
        ↓
FROZEN
```

以後は `Observer`、`KnowledgeGateway`、`ProjectPlanner`、`ProjectManager`、`ProjectExecutor` の公開契約を原則維持し、Full版の機能は内部実装または Adapter の追加として接続します。
