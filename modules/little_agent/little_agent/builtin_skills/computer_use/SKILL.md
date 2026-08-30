# computer_use

## Description
マウスとキーボードでPCを操作する。画面を見て決めた操作（クリック、文字入力、キー操作）を実機に反映する。`screen_capture` で画面を「見て」、このスキルで「操作」する。

## When to use
- ユーザーが「クリックして」「ここに入力して」「このボタンを押して」と画面操作を依頼したとき
- アプリやブラウザをマウス/キーボードで操作する必要があるとき
- 画面を確認しながら一連の操作を進めたいとき

## Allowed tools
- get_screen_info
- move_mouse
- click
- type_text
- press_keys

## Setup
このスキルにはマウス/キーボード操作用のライブラリが必要です。

    pip install pyautogui

GUI セッションが必要です。ヘッドレス環境では操作できません。

## Instructions
- 操作の前に `take_screenshot`（screen_capture スキル）で現在の画面を確認する。
- クリック対象の座標を決める前に `get_screen_info` で実解像度とカーソル位置を確認する。
- 座標は **実画面ピクセル** で渡す。`take_screenshot` の画像が縮小されている場合（例: `Captured screen 1568x882`）、その画像上で読み取った座標に加えて `image_size`（例 `[1568, 882]`）を渡せば、ツール側が実解像度へ自動変換する。座標計算を自分でやらないこと。
- 1操作ごとに screenshot で結果を確認し、次の操作を決める（見る→操作→見る のループ）。
- `click` / `type_text` / `press_keys` は実行前確認が出る破壊的操作。意図を明確にしてから呼ぶ。
- キー操作は `press_keys` を使う。単一キーは `enter` / `tab` / `esc`、組み合わせは `ctrl+c` / `alt+tab` のように渡す。
- アルファベット入力時、IMEが「ひらがな」だと文字化けする問題は、`type_text` がASCIIテキストに対して自動でIMEを直接入力（英数）へ切り替えるため通常は解消される。切り替えたくない場合は `force_english: false` を渡す。入力後に元のIME状態へ戻したい場合は `restore_ime: true`。
- 日本語など非ASCIIの入力は `type_text` では正しく入らないことがある。その場合はユーザーに伝えるか、クリップボード経由（`ctrl+v`）などの代替を検討する。
- 操作が暴走した場合、ユーザーはマウスを画面の隅へ動かすと中断できる（failsafe）。
- このスキルは実機を直接操作する。対象ウィンドウが正しいか、入力フォーカスが合っているかを screenshot で確認してから進める。
