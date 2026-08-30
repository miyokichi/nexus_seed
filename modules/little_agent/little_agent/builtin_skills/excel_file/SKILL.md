# excel_file

## Description
Excel `.xlsx` ファイルの読み取りと、表データからの簡易 `.xlsx` 作成を支援する。

## When to use
- ユーザーがExcelファイルの内容確認、要約、表の読み取りを依頼したとき
- 行列データやCSV風テキストからExcelファイルを作りたいとき
- 外部ライブラリなしで `.xlsx` を扱いたいとき

## Allowed tools
- read_excel
- write_excel

## Instructions
- 対象は `.xlsx` ファイルとする。古い `.xls` 形式は非対応。
- `read_excel` はセルの値をテキスト化して返す。書式、グラフ、画像、数式計算結果の完全再現は保証しない。
- `write_excel` は既存ブックの編集ではなく、新しい単一シート `.xlsx` を作成する。
- 既存ファイルへ書き込む場合は、上書きの可能性をユーザーに分かるようにする。
