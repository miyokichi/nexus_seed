# Outlook メール抽出スキル

## Description

Outlook のメール（ライブ Outlook、.pst ファイル）を **件名** と **送信元** でフィルタして抽出し、CSV または JSON として保存します。

## When to use

- Outlook から特定のキーワードを含むメールを抽出したい場合
- .pst アーカイブファイルからメールを検索・抽出したい場合
- 送信元を指定してメールを絞り込みたい場合
- 抽出結果を CSV/JSON でエクスポートしたい場合

## Allowed tools

- extract_emails

## Instructions

### 1. 環境確認

1. Windows かつ Outlook がインストールされていることを確認してください。
2. Python に `pywin32` がインストールされていることを確認してください。
   ```powershell
   pip install pywin32
   ```
3. アーキテクチャの整合性に注意してください（32-bit Outlook なら 32-bit Python、64-bit Outlook なら 64-bit Python）。

### 2. スクリプトの実行

Python スクリプト `scripts/extract_emails.py` を実行します。

#### 基本構文

```bash
python scripts/extract_emails.py --source-type <live|pst> [オプション]
```

#### 例: ライブ Outlook から抽出

```bash
python scripts/extract_emails.py ^
    --source-type live ^
    --subject "見積" ^
    --output "output\estimate.csv"
```

#### 例: .pst ファイルから抽出

```bash
python scripts/extract_emails.py ^
    --source-type pst ^
    --pst-path "C:\backup\data.pst" ^
    --sender "tanaka" ^
    --output "output\tanaka.json" ^
    --format json
```

#### Python からの呼び出し

```python
from scripts.extract_emails import extract_emails

results = extract_emails(
    source_type="pst",
    pst_path=r"C:\backup\archive.pst",
    subject_filter="お知らせ",
    sender_filter="news@company.com",
    output_path="notices.csv",
    output_format="csv",
    include_body=True,
)
print(f"{len(results)} 件のメールを抽出しました")
```

### 3. 出力結果

#### CSV（既定）

| カラム | 内容 |
|---|---|
| 送信元名前 | 差出人の名前 |
| 送信元メール | 差出人のメールアドレス |
| 受信者 | To / CC の一覧（カンマ区切り） |
| 件名 | メール件名 |
| 送信日時 | 送信日時 |
| 本文プレビュー | 冒頭 500 文字（`--include-body` でオン） |
| 添付あり | True / False |
| 添付数 | 添付ファイルの数 |

#### JSON

配列形式のオブジェクトで、各オブジェクトは CSV と同様のプロパティを持ちます。

### 4. 注意事項

- Outlook が起動している状態が推奨されます。
- Exchange Sync されていない古いメールは取得できない場合があります。
- 大量のデータを対象にすると処理に時間がかかることがあります。
- `.pst` ファイルは一度読み込まれると Outlook セッション中にマウントされたままになります。

### 機能

- 件名で部分一致フィルタ（大文字小文字区別なし）
- 送信元で部分一致フィルタ（名前・メールアドレス両方）
- CSV / JSON 形式で出力
- 添付ファイル有無・件数も抽出
- 本文プレビュー（500 文字）のオプション
- サブフォルダ再帰探索

### CLI オプション一覧

| オプション | 既定値 | 説明 |
|---|---|---|
| `--source-type` | `live` | `live` (アクティブ Outlook) / `pst` (.pst) |
| `--pst-path` | `-` | .pst ファイルのフルパス (`source-type=pst` のみ必須) |
| `--subject` | `-` | 件名に含まれる文字列フィルタ |
| `--sender` | `-` | 送信元に含まれる文字列フィルタ |
| `--output` | `output_emails.csv` | 出力ファイルパス |
| `--format` | `csv` | 出力フォーマット: `csv` / `json` |
| `--include-body` | `False` | 本文プレビュー (500 文字) を含む |
