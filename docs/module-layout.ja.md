# NEXUS SEED モジュール境界

NEXUS SEED本体は、独立した責務をApplication層で接続するcomposition rootです。
今回の整理は物理配置とimport方向だけを変更し、DB schema、Project lifecycle、
A2A message、Knowledge意味モデル、Approval仕様は変更していません。

## 正本の配置

| 所有者 | 正本 | 主な内容 |
|---|---|---|
| App | `nexus_seed/app/` | CLI、MVP flow、Knowledge loop、Project orchestration flow |
| Observer | `nexus_seed/modules/observer/` | Observation source、Ingress、manual/file adapter、Observer SQLite adapter |
| Knowledge | `nexus_seed/modules/knowledge/` | Ledger、model、projection、consolidation、principle、SQLite adapter |
| Planner | `nexus_seed/modules/planner/` | context assessment、goal bridge、MVP planner |
| Project Manager | `nexus_seed/modules/project_manager/` | Project lifecycle、Agent assignment、Workspace、A2A domain、SQLite adapter |
| Integration | `nexus_seed/integrations/` | webhook transport、A2A HTTP transport、LLM provider、Project Agent設定 |
| Policy | `nexus_seed/policy/approval/` | Human approval実装 |
| Contract | `nexus_seed/platform/contracts/` | 凍結済みMVP DTOとProtocol |

`nexus_seed/knowledge/`、`orchestrator/`、`workspace/`、`adapters/`、
`providers/`、`ingress/`、および移動済みの`storage/*`は、既存利用者を壊さない
ための薄い互換facadeです。新規コードは上表の正本をimportします。

## 意図的に残した共通領域

- `nexus_seed/core/` と `nexus_seed/runtime/` は、固定された6 primitives、永続化、
  Process復元で使う安定import pathです。実装は移動せず、`platform/core` と
  `platform/runtime` から公開します。
- `nexus_seed/storage/` にはEvent、Process、Continuation、Timerなど、単一domain
  moduleが所有できないdurable Runtime storeを残します。
- `nexus_seed/processes/resources.py` はObserver、Resource、Runtimeを横断する既存
  Process pipelineです。今回の対象4 moduleのどれかへ偽って所属させず、次の
  Resource module整理まで残します。
- `resources/`、`chat/`、`cockpit/`は今回の独立化対象外であり、意味変更を避ける
  ため移動していません。

## little_agent

`modules/little_agent/` は`main` branchを追跡するGit submoduleです。NEXUS SEEDの
wheelには含めず、相互のPython packageをimportしません。接続点は既存のA2A HTTP
transportだけです。

```text
Project Manager -> integrations/A2A -> little_agent A2A server
```

little_agentは1仕事の実行、Skill選択、Tool/LLM利用、Execution Result生成を所有し、
NEXUS SEED側はProject、長期状態、監査記録、Workspace grantを所有します。

## MVP互換

`nexus_seed/mvp/`に実装は残していません。各fileは新しい正本へのcompatibility
importであり、実際のcompositionは`nexus_seed/app/flows/mvp.py`です。したがって
凍結済みの公開interfaceとCLI entry pointを維持したまま、`mvp/`を将来削除できます。
