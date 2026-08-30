from __future__ import annotations

import threading


class StopController:
    """Global emergency-stop hotkey for AI computer operation.

    The listener is only active while ``arm()`` is in effect (i.e. while the
    agent is executing a turn), so it never intercepts keys while the user is
    typing at the prompt. When the hotkey fires, ``triggered`` becomes True and
    the agent loop aborts between tool calls.

    If ``pynput`` is not installed the controller degrades gracefully: no hotkey
    is registered and ``triggered`` stays False. The user can still abort with
    the pyautogui mouse-corner failsafe or Ctrl+C.
    """

    def __init__(self, hotkey: str) -> None:
        self.hotkey = hotkey
        self._event = threading.Event()
        self._listener: object | None = None
        self._available: bool | None = None

    @property
    def triggered(self) -> bool:
        return self._event.is_set()

    def reset(self) -> None:
        self._event.clear()

    def arm(self) -> None:
        """Start listening for the stop hotkey. No-op if already armed."""
        if self._listener is not None:
            return
        try:
            from pynput import keyboard
        except ImportError:
            if self._available is None:
                self._available = False
                print(
                    "[hotkey] pynput が無いため停止ホットキーは無効です。"
                    "pip install pynput（マウスを画面隅へ動かす failsafe と Ctrl+C は使えます）。"
                )
            return
        self._available = True
        try:
            listener = keyboard.GlobalHotKeys({self.hotkey: self._on_activate})
            listener.start()
        except Exception as exc:  # noqa: BLE001 - bad hotkey spec or platform issue.
            print(f"[hotkey] 停止ホットキーを登録できませんでした ({self.hotkey}): {exc}")
            return
        self._listener = listener

    def disarm(self) -> None:
        """Stop listening for the stop hotkey."""
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.stop()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - best effort cleanup.
                pass

    def _on_activate(self) -> None:
        self._event.set()
