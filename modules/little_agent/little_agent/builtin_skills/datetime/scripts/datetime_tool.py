from __future__ import annotations

import json
import sys
from datetime import datetime

WEEKDAYS_JA = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"]


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))

        if tool == "get_datetime":
            result = get_datetime()
        else:
            result = {"ok": False, "content": f"Unknown tool: {tool}"}
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "content": f"datetime script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


def get_datetime() -> dict[str, object]:
    now = datetime.now().astimezone()
    weekday_ja = WEEKDAYS_JA[now.weekday()]
    lines = [
        f"日時: {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"曜日: {weekday_ja}",
        f"週番号: 第{now.isocalendar().week}週",
        f"タイムゾーン: {now.tzname()}",
        f"ISO形式: {now.isoformat(timespec='seconds')}",
    ]
    return {"ok": True, "content": "\n".join(lines)}


if __name__ == "__main__":
    raise SystemExit(main())
