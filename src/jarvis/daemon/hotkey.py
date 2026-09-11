"""Global hotkey listener built on the Win32 RegisterHotKey API (no extra deps)."""

from __future__ import annotations

import ctypes
import re
import threading
from ctypes import wintypes
from typing import Callable

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "meta": MOD_WIN,
    "super": MOD_WIN,
}

NAMED_KEYS = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "home": 0x24,
    "end": 0x23,
    "insert": 0x2D,
    "delete": 0x2E,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
}


class HotkeyError(ValueError):
    """Raised when a hotkey spec cannot be parsed."""


def parse_hotkey(spec: str) -> tuple[int, int]:
    """``"ctrl+alt+j"`` -> (modifiers, virtual-key code)."""

    parts = [part.strip().lower() for part in re.split(r"\s*\+\s*", spec) if part.strip()]
    if not parts:
        raise HotkeyError("热键不能为空，例如 ctrl+alt+j")

    modifiers = 0
    key: str | None = None
    for part in parts:
        if part in MODIFIERS:
            modifiers |= MODIFIERS[part]
        else:
            key = part

    if key is None:
        raise HotkeyError(f"热键 '{spec}' 缺少主键，例如 ctrl+alt+j")
    if not modifiers:
        raise HotkeyError(f"热键 '{spec}' 缺少修饰键（ctrl / alt / shift / win）")

    if len(key) == 1 and (key.isalpha() or key.isdigit()):
        vk = ord(key.upper())
    elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", key):
        vk = 0x70 + int(key[1:]) - 1
    elif key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    else:
        raise HotkeyError(f"无法识别的按键 '{key}'")
    return modifiers, vk


def describe(spec: str) -> str:
    parts = [part.strip() for part in re.split(r"\s*\+\s*", spec) if part.strip()]
    pretty = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "meta": "Win"}
    return "+".join(pretty.get(part.lower(), part.upper()) for part in parts)


class GlobalHotkey:
    """Press-the-keys-and-JARVIS-appears, implemented in a worker thread."""

    def __init__(self, spec: str, callback: Callable[[], None], hotkey_id: int = 0xA1) -> None:
        self.spec = spec
        self.callback = callback
        self.hotkey_id = hotkey_id
        self.modifiers, self.vk = parse_hotkey(spec)
        self.error: str | None = None
        self.registered = False
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None

    # ------------------------------------------------------------------ control
    def start(self) -> "GlobalHotkey":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._run, name="jarvis-hotkey", daemon=True)
        self._thread.start()
        for _ in range(50):  # wait up to ~0.5s for registration
            if self.registered or self.error:
                break
            threading.Event().wait(0.01)
        return self

    def stop(self) -> None:
        if self._thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = None
        self.registered = False

    # --------------------------------------------------------------------- loop
    def _run(self) -> None:
        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]

        self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
        if not user32.RegisterHotKey(None, self.hotkey_id, self.modifiers, self.vk):
            self.error = f"注册 {self.spec} 失败，可能已被其他程序占用"
            return
        self.registered = True

        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY and message.wParam == self.hotkey_id:
                    try:
                        self.callback()
                    except Exception:  # noqa: BLE001 - one bad callback must not kill the listener
                        pass
        finally:
            user32.UnregisterHotKey(None, self.hotkey_id)
            self.registered = False
