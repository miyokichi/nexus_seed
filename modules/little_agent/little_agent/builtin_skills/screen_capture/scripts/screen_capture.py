from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path
from typing import Any

# Downscale wide screenshots before sending them to the model to limit tokens.
_MAX_WIDTH = 1568


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))
        workspace = Path(str(payload["workspace"])).resolve()
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")

        if tool == "take_screenshot":
            result = take_screenshot(workspace, arguments)
        else:
            result = {"ok": False, "content": f"Unknown screen tool: {tool}"}
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"Screen capture script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


def take_screenshot(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    captured = capture_image(arguments.get("region"))
    if "error" in captured:
        return {"ok": False, "content": captured["error"]}
    image = downscale(captured["image"], _MAX_WIDTH)

    saved_note = ""
    save_path = str(arguments.get("save_path") or "").strip()
    if save_path:
        target = safe_workspace_path(workspace, save_path)
        if target is None:
            return {"ok": False, "content": f"save_path is outside workspace: {save_path}"}
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, format="PNG")
        saved_note = f" (saved to {target.relative_to(workspace)})"

    data_uri = png_data_uri(image)
    return {
        "ok": True,
        "content": f"Captured screen {image.width}x{image.height}{saved_note}.",
        "images": [data_uri],
    }


def capture_image(region: Any) -> dict[str, Any]:
    """Return {"image": PIL.Image} or {"error": str}."""
    try:
        import mss
        from PIL import Image
    except ImportError:
        return {"error": "mss と Pillow が必要です。pip install mss Pillow"}

    bbox = parse_region(region)
    try:
        with mss.mss() as sct:
            if bbox is None:
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            else:
                monitor = {"left": bbox[0], "top": bbox[1], "width": bbox[2], "height": bbox[3]}
            shot = sct.grab(monitor)
    except Exception as exc:  # noqa: BLE001 - common when headless / no display.
        return {"error": f"Could not capture the screen (no display?): {exc}"}

    image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return {"image": image}


def parse_region(region: Any) -> list[int] | None:
    if not region:
        return None
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        raise ValueError("region must be [x, y, width, height].")
    return [int(value) for value in region]


def downscale(image: Any, max_width: int) -> Any:
    if image.width <= max_width:
        return image
    ratio = max_width / float(image.width)
    new_size = (max_width, max(1, int(image.height * ratio)))
    return image.resize(new_size)


def png_data_uri(image: Any) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def safe_workspace_path(workspace: Path, raw: str) -> Path | None:
    path = Path(raw)
    if not path.is_absolute():
        path = workspace / path
    try:
        resolved = path.resolve()
    except Exception:  # noqa: BLE001
        return None
    if workspace not in [resolved, *resolved.parents]:
        return None
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
