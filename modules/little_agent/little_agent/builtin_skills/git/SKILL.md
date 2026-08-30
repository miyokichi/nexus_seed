# git

## Description
Gitリポジトリの状態確認、変更のステージング、コミットを支援する。

## When to use
- ワークスペースのgit状態（変更ファイル一覧）を確認したいとき
- 変更差分（diff）を確認したいとき
- コミット履歴を確認したいとき
- ファイルをステージングしてコミットしたいとき

## Allowed tools
- git_status
- git_diff
- git_log
- git_add
- git_commit

## Instructions
- コミット前は必ず `git_status` で状態を確認する。
- コミットメッセージは変更内容を簡潔に表す英語または日本語で書く。
- `git_add` でステージングしてから `git_commit` でコミットする。
- 差分が大きい場合は `path` を絞って `git_diff` を呼ぶ。
- push や branch 操作はこのスキルのスコープ外。
