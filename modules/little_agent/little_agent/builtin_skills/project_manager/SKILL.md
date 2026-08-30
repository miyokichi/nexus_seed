# project_manager

## Description
プロジェクトとタスクの統合管理。単発TODOから、AIタスクと人間タスクの依存関係付きワークフロー(DAG)まで data/projects.json で一元管理し、進捗の記録とブラウザでの可視化・編集を支援する。計画、段取り、工程管理、承認フロー、タスク分解、進捗確認に対応する。

## When to use
- タスク、TODO、やることリストの追加、確認、完了、削除を頼まれたとき
- 複数ステップの計画、プロジェクトの段取り、工程を立てたいとき
- AIの作業と人間の承認、レビュー、作業が混ざる進行を管理したいとき
- プロジェクト、ワークフロー、依存関係、進捗、可視化、ビューア、ダッシュボードが話題のとき
- タスクの状態(着手、完了、失敗、スキップ)や内容(担当、期限、優先度、依存)を記録・変更したいとき

## Allowed tools
- create_project
- add_task
- update_task
- update_task_status
- add_task_comment
- show_project
- list_projects
- list_tasks
- delete_task
- delete_project
- open_project_viewer

## Instructions
- 単発のTODO(「〜のタスク追加して」)は project_id なしの add_task で Inbox に入れる。段取りが必要なゴールは create_project でタスク分解する。
- ゴールは3〜10個のタスクに分解する。1タスクは1回の作業で完了できる粒度にする。
- assignee の判断基準: エージェントのツールで完結する作業(調査、文書作成、コード、ファイル操作)は "ai"。実世界の作業、外部システムの操作、意思決定、承認、レビューは "human"。承認が必要な箇所は独立した human タスク(承認ゲート)として間に挟む。
- 特定の人に割り当てる場合は human タスクにして assignee_name にその人の名前(例: 山田さん)を入れる。名前は表示用で、タスクは通常どおり human タスクとして扱われる。ai タスクに名前は付けない。
- create_project では各タスクに一時キー key を付け、depends_on は key で参照する。登録後は応答に含まれる実IDを使って操作する。
- depends_on は順序が本当に必要な場合だけ張る。並列にできるタスクへ不要な依存を張らない。
- 作成後はタスク一覧(ID付き)を要約して見せ、open_project_viewer でブラウザから確認・編集できることを案内する。
- タスクの内容変更(タイトル、担当、期限、優先度、依存)は update_task、状態変更は update_task_status を使う。
- AIタスクを実行するときのプロトコル: 着手直前に update_task_status で running にする → 作業する → 完了直後に done と result(成果の1〜2文)を記録する。失敗したら failed と理由を記録する。
- 作業が長い・複数段階のタスクでは、節目ごとに add_task_comment で進捗コメント(いま何をしたか、次に何をするか)を残す。人間もビューアからコメントを書けるので、show_project や一覧で新しいコメントを見たら内容を踏まえて動く。
- 人間もビューアからプロジェクト作成・タスク追加・編集・完了ができる。ビューアで行われた変更は completed_via / created_via が "viewer" になる。次のターンでは最新状態を show_project で確認してから動く。
- 対象の project_id / task_id が曖昧なときは、先に list_projects や show_project で確認する。
- delete_task / delete_project は元に戻せないため、確認プロンプトを尊重する。
