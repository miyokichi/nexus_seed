# Architecture Inventory（機能棚卸し）

Goal駆動統合フェーズの開始前に、既存実装を再利用するために作成した棚卸しです。
`nexus_seed/`の全moduleを、次のループ上のいずれか1領域へ分類しています。

```text
Goal -> Project -> World -> Work/Task -> Capability -> Execution -> Evaluation -> Goal
```

1〜6がループ本体です。Runtime Infrastructureは6領域すべてを支える基盤で、
同列には置きません。Interface / Adapterは人間と外界がループへ触れる境界です。

## 1. Goal / Project Management

| Module | 役割 |
| --- | --- |
| `control/models.py` | `Goal`、priority、constraints、`HumanIdentity`、`Command` |
| `control/service.py` | `ConsoleService`。認可済み`/goal`・`/task` command。Goal作成と同時にProjectを作る |
| `control/parser.py`, `control/adapters.py` | 明示commandの決定論的parse、channel mapping |
| `storage/control_store.py` | Goal / identity / command / resultの永続化 |
| `projects/lifecycle.py` | Goal idから導出するProject identity |
| `projects/models.py`, `projects/projections.py` | `Project` / `ProjectSituation` / status導出 |
| `presence/*` | Phase 6のSelf / Master、Goalの下にある永続Intention |
| `processes/persistent_being.py` | Intention維持・Attention・Reflectionを通常Processとして実行 |

## 2. World Model / Observation

| Module | 役割 |
| --- | --- |
| `world/observation.py`, `world/state_delta.py` | 何を読んだか／何を変えようとするか |
| `world/provenance.py` | 現在の事実からraw Eventまで遡る |
| `storage/state_store.py` | 追記履歴と再構築可能な現在ビュー |
| `storage/observation_store.py`, `storage/state_delta_store.py` | 上記2種のjournal |
| `processes/semantic.py` | `interpret_event` → `apply_state_delta` → `state_changed` |
| `processes/llm_interpret.py`, `intelligence/*` | LLM解釈の境界（proposal・validation・policy） |
| `resources/*`, `storage/resource_store.py`, `processes/resources.py` | 文書を版付き・抽出済みのWorld内容として保持 |

## 3. Work Planning

| Module | 役割 |
| --- | --- |
| `work/work_requirement.py`, `work/work_match.py` | 必要な作業と、既に満たされているかの判定 |
| `work/rules.py` | 「この変化はこの作業を要求する」決定論ルール |
| `work/impact.py`, `work/trace.py` | 影響記録、Processが動いている理由の追跡 |
| `processes/work_intelligence.py` | `impact → work_required → match → missing → spawn → satisfied` |
| `processes/control.py` | `evaluate_goal`。Goal + criteria + World → gap → Work（領域6も担当） |
| `storage/work_requirement_store.py` | 永続的な「必要」 |
| `planning/*`, `processes/planning.py`, `storage/plan_store.py` | 単一Processで足りない場合の複数Process合成 |
| `decision/*`, `processes/decision.py`, `storage/decision_store.py` | Plan評価・選択・有界replanning |

## 4. Capability Management

| Module | 役割 |
| --- | --- |
| `capabilities/*`, `storage/capability_store.py` | 何ができるか、必要との照合 |
| `extension/*`, `processes/extension.py`, `storage/extension_store.py` | Gap分析と取得提案 |
| `construction/*`, `processes/construction.py`, `storage/construction_store.py` | Sandbox内構築と検証 |
| `installation/*`, `processes/installation.py`, `storage/installation_store.py` | 承認付きProduction activationとrollback |
| `autonomy/*`, `processes/autonomy.py`, `storage/autonomy_store.py` | `AUTO` / `REVIEW_REQUIRED` / `FORBIDDEN`とBudgetを持つ有界coordinator |

## 5. Execution

| Module | 役割 |
| --- | --- |
| `providers/*`, `storage/provider_store.py` | 内部Process・Directory Skill・外部AgentのProvider federation（`providers/a2a.py`がA2A境界、`providers/skills.py`がSkillのloader/importer） |
| `backends/base.py`, `backends/llm.py` | 交換可能な推論エンジン |
| `backends/action.py`, `actions/*`, `processes/actions.py` | 外界に触れる唯一の経路（permission・risk付き） |
| `storage/action_*.py` | Proposal / decision / execution |
| `processes/work_intelligence.py::resistance_check`, `processes/demo_resistance.py` | Demo用のWork Process（Legacy参照） |

## 6. Evaluation / Replanning

Evaluationは専用moduleを持ちません。既存の3つの仕組みの組み合わせです。
これが「無いように見えていた」理由でもあります。

| 仕組み | 実装 |
| --- | --- |
| Taskは本当に完了したか | `core/process.py::satisfy_work` + `WorkRequirement.completion_criteria` |
| 現在のWorldでGoalは満たされたか | `processes/control.py::evaluate_goal`（`work_satisfied` / `state_changed` / `goal_evaluation_requested`で起動） |
| 別のやり方はあるか | `processes/decision.py`のreplanning、`processes/work_intelligence.py::reconcile_blocked_work` |
| Projectは完了したか | `projects/projections.py::project_status` |

## Runtime Infrastructure（6領域を支える基盤）

`core/*`（Event / Process / State / Context / Continuation / effects）・
`runtime/*`（router・scheduler・executor・resolver・drain・clock・services）・
`delivery/*`と`storage/event_delivery_store.py`（durable delivery）・
`context/*`（宣言に基づくactivation view）・`storage/database.py`と汎用store
（`event` / `process` / `continuation` / `timer` / `join` / `activation` /
`context_snapshot` / `llm_invocation` / `adapter_checkpoint`）・`llm_config.py`・
`federation_config.py`（外部Agent RuntimeとSkill rootの設定）。

## Interface / Adapter

`adapters/*`・`ingress/*`（外界をEventにする）・`ingress_cli.py`・
`operations.py`・`app.py`（CLIと常駐サーバー）・`cockpit/*`（read-only human
interface）・`chat/*`（read-only Project Chat）・`storage/chat_store.py`・
`storage/ingress_receipt_store.py`。

## Orchestration（本フェーズで追加、意図的に薄い層）

`orchestration/models.py`・`orchestration/loop.py`はGoalがループのどこに
いるかを既存領域から再構成するだけで、何も所有しません。
`orchestration/processes.py`は「自力で進められない」ことをEventとして表明する
Processを1つだけ持ちます。

## Unclassified と判断結果

| 対象 | 所見 | 判断 |
| --- | --- | --- |
| `processes/demo_resistance.py`、`resistance_check`、`processes/work_intelligence.py`の`DEFAULT_WAFER` | wafer / resistanceのdemo domainがwork intelligence側に埋まっている | Legacyとして維持。suspend/resumeの受入シナリオであり、Phase 1〜2の多数のテストが依存する。将来的にdemo packageへ移すべきだが、本フェーズでは既存テストを変更しない方針のため移動しない |
| `work/rules.py` | 同じdemo domainの決定論ルール（`expected_work_types`、capability / I/O表） | 維持。deployment側がルールを宣言する継ぎ目であり、Goal経路はこれを使わない。第2のルールエンジンは追加しない |
| `work/impact.py` | `Impact`は`work/__init__`とテストからのみ参照され、handlerは生成していない（`impact_analysis`は`work/rules.py`を直接使う） | 削除せず、文書化された domain data として維持。`tests/test_impact_analysis.py`が実行しており、第2の影響モデルは作らない |
| `providers/skills.py` | Directory skill import。明示登録経由でのみ到達 | 維持。外部AgentをExecution Providerへ統合する経路であり、本フェーズはAgent frameworkを追加せずここを使う |
| `demo.py`、`nexus_seed/processes/__init__.py`のre-export | Phase 1の実行可能demo | 維持（READMEに記載済み） |

本フェーズでの削除はありません。上記の領域はすべて既存実装であり、統合で
追加したのはそれらの上に載る調整層だけです。
