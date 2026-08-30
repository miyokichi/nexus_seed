# shared/ — 共通資料

タスクをまたいで使う共通資料（テンプレート・参考資料・過去の成果物など）を置くフォルダです。
`workspace_harness` スキルの AI は、タスク着手時に `search_shared` でここを自律的に検索し、
関連する素材を見つけて `read_shared` で確認し、`update_task` の `materials_add` でタスクに紐づけます。

保存場所は `LITTLE_AGENT_SHARED_DIR` で変更できます（ワークスペース外の絶対パスも可）。

## 目安の構成

```
shared/
  templates/     繰り返し使う雛形（見積・請求・議事録など）
  references/    参考資料・ガイドライン・過去資料
```

- テキスト（.md/.txt/.csv など）はファイル名と中身の両方が検索対象になります。
- Office ファイル（.xlsx/.pptx/.docx）はファイル名で見つけ、中身は excel_file / ppt_file スキルで開きます。
