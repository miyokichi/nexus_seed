from __future__ import annotations

import json
import sys


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    arguments = payload.get("arguments") or {}
    text = str(arguments.get("text") or "")
    print(json.dumps({"ok": True, "content": text}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
