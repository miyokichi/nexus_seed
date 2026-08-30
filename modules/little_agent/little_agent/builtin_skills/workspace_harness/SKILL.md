# workspace_harness

## Description
人間とAIが同じディレクトリ上で協働するための、ファイルシステム型タスクハーネス。タスク1つ=1フォルダ(task.md を持つ)として扱い、ディレクトリ構造そのものを真実の源にする。上位のエリア(区分)フォルダは人間が統制し、AIはその中でタスクを作成・提案・更新し、共通資料フォルダを自律的に探索する。

## When to use
- タスクをフォルダ単位で管理したいとき、フォルダを見ながら作業や段取りを進めたいとき
- 「どのエリアに何のタスクがあるか」を把握したい、タスクを新設・提案・更新したいとき
- 上位のフォルダ構成(エリア)を人間が管理し、AIはその中で動く運用をしたいとき
- 共通資料、テンプレート、過去資料など別フォルダの素材を探して使いたいとき
- ハーネス、ワークスペース、タスクフォルダ、area、共通資料が話題のとき

（依存関係付きDAGや data/projects.json でのプロジェクト管理は project_manager を使う。こちらは「フォルダ=タスク」の運用向け。）

## Allowed tools
- harness_overview
- list_task_folders
- read_task_folder
- create_task_folder
- update_task_folder
- add_task_note
- propose_area
- search_shared
- read_shared

## Instructions
- ディレクトリ規約:
  - `tasks/<area>/<task-slug>/task.md` … タスク1つ=1フォルダ。`task.md` はフロントマター(status, assignee, priority, due, tags, materials, created, updated)と本文(目的・進捗ログ)を持つ。
  - `tasks/<area>/` … 上位エリア。**人間が統制する領域**。並び順・意味・命名は人間が決める。
  - `shared/` … 共通資料(テンプレート・参考・過去資料)。`LITTLE_AGENT_SHARED_DIR` で変更可。
  - 保存先は `LITTLE_AGENT_TASKS_DIR`(既定 `tasks`)で変更可。
- **最初に harness_overview** で全体像(どんなエリアがあり、各エリアに何のタスクがどの状態であるか、共通資料の有無)を掴んでから動く。曖昧なときは list_task_folders / read_task_folder で確認する。
- **上位エリアは作らない・改名しない・消さない**。既存エリアが合わなければ create_task_folder は失敗する。その場合は `propose_area` で新エリアを提案として記録し、ユーザーに「tasks/<name>/ を作ってよいか」を確認する。フォルダ自体は人間が作る。
- タスクの新設: 既存エリア内なら create_task_folder で直接作る(確認プロンプトが出る)。ユーザーの承認を前提にしたい提案タスクは `status: proposed` で作り、ユーザーがOKしたら update_task_folder で `todo` に上げる。
- 担当の判断: エージェントのツールで完結する作業(調査・文書作成・コード・ファイル操作)は assignee=ai。実世界の作業・意思決定・承認・レビューは assignee=human。特定の人には assignee=human + assignee_name(例: 山田さん)。
- AIタスクの実行プロトコル: 着手時に update_task_folder で status=doing → 作業 → 完了時に status=done。人間の承認待ちにするときは status=review。行き詰まったら status=blocked にして add_task_note に理由を書く。
- 節目ごとに add_task_note で進捗(いま何をしたか・次に何をするか)を task.md に残す。人間も同じ task.md を直接編集・追記できるので、read_task_folder で最新の本文とログを読んでから動く。
- **共通資料の自律探索**: タスクに着手したら、まず search_shared でそのタスクに関係しそうなテンプレートや過去資料を `shared/` から探す(ユーザーに聞く前に自分で探す)。見つけた素材は read_shared で中身を確認し、使うものは update_task_folder の materials_add でタスクに紐づける。Officeファイル(.xlsx/.pptx/.docx)は excel_file / ppt_file スキルで開く。
- タスクの成果物は `tasks/<area>/<slug>/outputs/`、作業メモは `notes/` に置く(core の write_file 等を使う)。
- タスクの削除は行わず、不要になったら status=cancelled にする(履歴を残す)。
