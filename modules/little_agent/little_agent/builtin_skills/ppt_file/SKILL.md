# ppt_file

## Description
PowerPoint `.pptx` ファイルのテキスト読み取りと、タイトル・箇条書きからの簡易 `.pptx` 作成を支援する。

## When to use
- ユーザーがPowerPointファイルの内容確認、要約、スライドテキスト抽出を依頼したとき
- タイトルと箇条書きから簡単なPowerPoint資料を作りたいとき
- 外部ライブラリなしで `.pptx` を扱いたいとき

## Allowed tools
- read_ppt
- write_ppt

## Instructions
- 対象は `.pptx` ファイルとする。古い `.ppt` 形式は非対応。
- `read_ppt` はスライド内のテキストを抽出する。画像、図形、配置、ノート、アニメーションの完全再現はしない。
- `write_ppt` は既存ファイルの編集ではなく、新しい簡易 `.pptx` を作成する。
- 複雑なデザインが必要な場合は、このSkillの出力を下書きとして扱う。
