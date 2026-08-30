# tasks/ — 人間×AI タスクハーネス

このフォルダは `workspace_harness` スキルが使う、ファイルシステム型のタスク置き場です。
**ディレクトリ構造そのものがタスクの真実の源**です。

## 規約

```
tasks/
  <area>/                 上位エリア（区分）。★人間が統制する領域★
    <task-slug>/          タスク1つ = 1フォルダ
      task.md             タスク定義（フロントマター + 目的 + 進捗ログ）
      outputs/            成果物
      notes/              作業メモ
  PROPOSALS.md            AIが提案した新エリア（人間が判断して作る）
```

## 役割分担

- **上位エリア（`tasks/<area>/`）は人間が管理します。** 並び順・意味・命名はあなたが決めてください。AIはエリアを作成・改名・削除しません。既存エリアが合わないときは AI が `propose_area` で `PROPOSALS.md` に提案を書き、あなたが承認して初めてフォルダを作ります。
- **タスクフォルダはAIも作れます。** 既存エリアの中に `create_task_folder` で作成します。承認前提の提案は `status: proposed` で作られるので、あなたがOKしたら `todo` に上げます。
- **task.md は人間もAIも編集できます。** 状態や進捗ログを両者が読み書きして協働します。

## task.md のフロントマター

| キー | 意味 |
| --- | --- |
| `title` | タスク名 |
| `status` | `todo` / `doing` / `review`（人間の承認待ち）/ `blocked` / `done` / `cancelled` / `proposed`（AI提案・未承認） |
| `assignee` | `ai` / `human` |
| `assignee_name` | human タスクの担当者名（例: 山田さん） |
| `priority` | `low` / `normal` / `high` |
| `due` | 期限 |
| `tags` | カンマ区切りのタグ |
| `materials` | このタスクに紐づけた `shared/` の資料パス |
| `created` / `updated` | 作成・更新日時 |

共通資料は `shared/`（`LITTLE_AGENT_SHARED_DIR` で変更可）に置きます。AIは着手時に
`search_shared` でそこを自律的に探し、使う資料を `update_task_folder` の materials_add で
task.md の `materials` に紐づけます。
