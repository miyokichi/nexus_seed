from __future__ import annotations

import json
import sys
import time
from typing import Any


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")

        handler = HANDLERS.get(tool)
        if handler is None:
            result = {"ok": False, "content": f"Unknown computer tool: {tool}"}
        else:
            result = handler(arguments)
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"computer_use script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


def _load_pyautogui() -> Any:
    try:
        import pyautogui
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError("pyautogui が必要です。pip install pyautogui") from exc
    # Move the mouse to a screen corner to abort a runaway session.
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.0
    return pyautogui


def _to_real_coords(pyautogui: Any, x: Any, y: Any, image_size: Any) -> tuple[int, int]:
    """Map a coordinate the model saw to real screen pixels (tool-side only).

    If ``image_size`` ([width, height] of the screenshot the model based its
    coordinate on) is given and differs from the real resolution, scale the
    coordinate here so the model never has to do the math.
    """
    real_w, real_h = pyautogui.size()
    rx, ry = int(round(float(x))), int(round(float(y)))
    if image_size:
        if not isinstance(image_size, (list, tuple)) or len(image_size) != 2:
            raise ValueError("image_size must be [width, height].")
        iw, ih = float(image_size[0]), float(image_size[1])
        if iw <= 0 or ih <= 0:
            raise ValueError("image_size values must be positive.")
        rx = int(round(float(x) * real_w / iw))
        ry = int(round(float(y) * real_h / ih))
    # Keep the cursor on screen.
    rx = max(0, min(rx, real_w - 1))
    ry = max(0, min(ry, real_h - 1))
    return rx, ry


def get_screen_info(_arguments: dict[str, Any]) -> dict[str, Any]:
    pyautogui = _load_pyautogui()
    width, height = pyautogui.size()
    cx, cy = pyautogui.position()
    return {
        "ok": True,
        "content": (
            f"Screen resolution {width}x{height}. Cursor at ({cx}, {cy}). "
            f"Pass coordinates in real screen pixels (0..{width - 1}, 0..{height - 1}). "
            f"If you read a coordinate off a downscaled screenshot, also pass image_size "
            f"so the tool can convert it."
        ),
    }


def move_mouse(arguments: dict[str, Any]) -> dict[str, Any]:
    pyautogui = _load_pyautogui()
    if "x" not in arguments or "y" not in arguments:
        raise ValueError("move_mouse requires x and y.")
    x, y = _to_real_coords(pyautogui, arguments["x"], arguments["y"], arguments.get("image_size"))
    duration = float(arguments.get("duration") or 0.0)
    pyautogui.moveTo(x, y, duration=max(0.0, duration))
    return {"ok": True, "content": f"Moved mouse to ({x}, {y})."}


def click(arguments: dict[str, Any]) -> dict[str, Any]:
    pyautogui = _load_pyautogui()
    button = str(arguments.get("button") or "left").lower()
    if button not in {"left", "right", "middle"}:
        raise ValueError("button must be left, right, or middle.")
    clicks = int(arguments.get("clicks") or 1)
    if clicks < 1:
        raise ValueError("clicks must be >= 1.")

    if "x" in arguments and "y" in arguments:
        x, y = _to_real_coords(pyautogui, arguments["x"], arguments["y"], arguments.get("image_size"))
        pyautogui.click(x=x, y=y, clicks=clicks, button=button)
        where = f"at ({x}, {y})"
    else:
        pyautogui.click(clicks=clicks, button=button)
        cx, cy = pyautogui.position()
        where = f"at current position ({cx}, {cy})"
    return {"ok": True, "content": f"{button} click x{clicks} {where}."}


def type_text(arguments: dict[str, Any]) -> dict[str, Any]:
    pyautogui = _load_pyautogui()
    text = arguments.get("text")
    if not isinstance(text, str) or text == "":
        raise ValueError("type_text requires a non-empty text string.")
    interval = float(arguments.get("interval") or 0.0)

    # On Windows, a Japanese IME in kana mode garbles ASCII input. Switch the
    # foreground window's IME to direct (alphanumeric) input first so letters
    # type correctly. Default: do this whenever the text is plain ASCII.
    force_english = arguments.get("force_english")
    if force_english is None:
        force_english = text.isascii()
    ime_note = ""
    prev_open: int | None = None
    if force_english:
        prev_open = _set_ime_open(False)
        if prev_open == 1:
            ime_note = " (IME switched to direct input)"
            time.sleep(0.05)  # let the IME settle before typing

    pyautogui.write(text, interval=max(0.0, interval))

    # Optionally restore the IME to its previous (e.g. Japanese) state.
    if force_english and prev_open == 1 and bool(arguments.get("restore_ime", False)):
        _set_ime_open(True)

    note = ""
    if not text.isascii():
        note = " (Note: pyautogui.write may not enter non-ASCII/IME text reliably; consider clipboard paste.)"
    return {"ok": True, "content": f"Typed {len(text)} characters.{ime_note}{note}"}


def _set_ime_open(open_status: bool | None) -> int | None:
    """Get and optionally set the foreground window's IME open status (Windows).

    Returns the previous open status (1 = IME on / e.g. kana, 0 = off / direct
    input) or None when unavailable (non-Windows, no IME, or no focused window).
    Pass ``open_status=None`` to only read, ``False`` to switch to direct
    alphanumeric input, ``True`` to turn the IME back on.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return None
    if not hasattr(ctypes, "windll"):
        return None  # not Windows

    WM_IME_CONTROL = 0x0283
    IMC_GETOPENSTATUS = 0x0005
    IMC_SETOPENSTATUS = 0x0006

    try:
        user32 = ctypes.windll.user32
        imm32 = ctypes.windll.imm32
        user32.GetForegroundWindow.restype = wintypes.HWND
        imm32.ImmGetDefaultIMEWnd.argtypes = [wintypes.HWND]
        imm32.ImmGetDefaultIMEWnd.restype = wintypes.HWND
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.SendMessageW.restype = wintypes.LPARAM

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        ime_hwnd = imm32.ImmGetDefaultIMEWnd(hwnd)
        if not ime_hwnd:
            return None
        prev = int(user32.SendMessageW(ime_hwnd, WM_IME_CONTROL, IMC_GETOPENSTATUS, 0))
        if open_status is not None:
            user32.SendMessageW(ime_hwnd, WM_IME_CONTROL, IMC_SETOPENSTATUS, 1 if open_status else 0)
        return prev
    except Exception:  # noqa: BLE001 - never let IME handling break typing.
        return None


def press_keys(arguments: dict[str, Any]) -> dict[str, Any]:
    pyautogui = _load_pyautogui()
    keys = arguments.get("keys")
    if isinstance(keys, str):
        # Accept "ctrl+c" style combos as well as a single key name.
        parts = [part.strip().lower() for part in keys.split("+") if part.strip()]
    elif isinstance(keys, (list, tuple)):
        parts = [str(part).strip().lower() for part in keys if str(part).strip()]
    else:
        raise ValueError("keys must be a string like 'ctrl+c' or an array of key names.")
    if not parts:
        raise ValueError("keys is empty.")

    if len(parts) == 1:
        pyautogui.press(parts[0])
    else:
        pyautogui.hotkey(*parts)
    return {"ok": True, "content": f"Pressed {'+'.join(parts)}."}


HANDLERS = {
    "get_screen_info": get_screen_info,
    "move_mouse": move_mouse,
    "click": click,
    "type_text": type_text,
    "press_keys": press_keys,
}


if __name__ == "__main__":
    raise SystemExit(main())
