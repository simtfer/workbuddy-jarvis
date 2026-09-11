"""Clipboard tools: read and write the Windows clipboard.

Implemented with the Win32 clipboard API through ``ctypes`` — the same trick the
global-hotkey and tray modules use — so this adds no dependency at all.

Two things make the Win32 clipboard fiddly and are handled here:

* **It is single-owner.** Only one process may hold it open at a time, so
  ``OpenClipboard`` is retried a few times before giving up, and the handle is
  always released in a ``finally`` block.
* **Handles are pointers.** On 64-bit Windows ``GetClipboardData`` /
  ``GlobalLock`` return pointers, and ctypes defaults to ``c_int`` (32-bit),
  which silently truncates them. Every prototype below therefore sets
  ``restype``/``argtypes`` explicitly.

Beyond plain text the reader also understands ``CF_HDROP``, i.e. the file list
produced by copying files in Explorer.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

from ..core.registry import ToolError

IS_WINDOWS = os.name == "nt"

CF_TEXT = 1
CF_OEMTEXT = 7
CF_UNICODETEXT = 13
CF_HDROP = 15

GMEM_MOVEABLE = 0x0002
# Another app grabbing the clipboard is normal and usually brief, so retry for
# roughly a second (growing delay) before giving up: long enough for a transient
# owner, short enough that a wedged clipboard reports back quickly.
OPEN_ATTEMPTS = 5
OPEN_BASE_DELAY = 0.05
OPEN_MAX_DELAY = 0.25

ERROR_ACCESS_DENIED = 5

_UNNAMED_FORMAT = 0xC000

_FORMAT_NAMES = {
    1: "CF_TEXT",
    2: "CF_BITMAP",
    3: "CF_METAFILEPICT",
    4: "CF_SYLK",
    5: "CF_DIF",
    6: "CF_TIFF",
    7: "CF_OEMTEXT",
    8: "CF_DIB",
    9: "CF_PALETTE",
    10: "CF_PENDATA",
    11: "CF_RIFF",
    12: "CF_WAVE",
    13: "CF_UNICODETEXT",
    14: "CF_ENHMETAFILE",
    15: "CF_HDROP",
    16: "CF_LOCALE",
    17: "CF_DIBV5",
}

if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.argtypes = []
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.argtypes = []
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    _user32.EnumClipboardFormats.restype = wintypes.UINT
    _user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClipboardFormatNameW.restype = ctypes.c_int

    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL
    _kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalSize.restype = ctypes.c_size_t

    _shell32.DragQueryFileW.argtypes = [
        wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT
    ]
    _shell32.DragQueryFileW.restype = wintypes.UINT
else:  # pragma: no cover - JARVIS is Windows-only, this keeps imports importable
    _user32 = _kernel32 = _shell32 = None


class _ClipboardLock:
    """Context manager around OpenClipboard/CloseClipboard."""

    def __enter__(self) -> "_ClipboardLock":
        if not IS_WINDOWS:
            raise ToolError("剪贴板工具只支持 Windows。")
        delay = OPEN_BASE_DELAY
        for _ in range(OPEN_ATTEMPTS):
            if _user32.OpenClipboard(None):
                return self
            time.sleep(delay)
            delay = min(delay * 1.8, OPEN_MAX_DELAY)
        code = ctypes.get_last_error()
        hint = (
            "另一个程序正占用着剪贴板（常见于截图 / 剪贴板管理 / 聊天工具）"
            if code == ERROR_ACCESS_DENIED
            else f"Win32 错误 {code}"
        )
        raise ToolError(f"打不开剪贴板：{hint}。等一两秒再试一次。")

    def __exit__(self, *_exc: object) -> bool:
        _user32.CloseClipboard()
        return False


# ------------------------------------------------------------------ locked ops
def _format_name(fmt: int) -> str:
    known = _FORMAT_NAMES.get(fmt)
    if known:
        return known
    if fmt >= _UNNAMED_FORMAT:
        buffer = ctypes.create_unicode_buffer(128)
        length = _user32.GetClipboardFormatNameW(fmt, buffer, 128)
        return buffer.value if length else f"fmt#{fmt}"
    return f"fmt#{fmt}"


def _formats_locked() -> list[str]:
    names: list[str] = []
    seen: set[int] = set()
    fmt = 0
    while True:
        fmt = _user32.EnumClipboardFormats(fmt)
        if not fmt or fmt in seen:
            break
        seen.add(fmt)
        names.append(_format_name(fmt))
    return names


def _clipboard_text_locked() -> str:
    for fmt in (CF_UNICODETEXT, CF_TEXT, CF_OEMTEXT):
        handle = _user32.GetClipboardData(fmt)
        if not handle:
            continue
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            continue
        try:
            if fmt == CF_UNICODETEXT:
                return ctypes.wstring_at(pointer)
            raw = ctypes.string_at(pointer)
            for encoding in ("mbcs", "utf-8", "gbk"):
                try:
                    return raw.decode(encoding)
                except (LookupError, UnicodeDecodeError):
                    continue
            return raw.decode("utf-8", "replace")
        finally:
            _kernel32.GlobalUnlock(handle)
    return ""


def _clipboard_files_locked() -> list[str]:
    """File paths copied in Explorer (CF_HDROP)."""

    handle = _user32.GetClipboardData(CF_HDROP)
    if not handle:
        return []
    count = _shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
    paths: list[str] = []
    for index in range(count):
        length = _shell32.DragQueryFileW(handle, index, None, 0)
        if not length:
            continue
        buffer = ctypes.create_unicode_buffer(length + 1)
        if _shell32.DragQueryFileW(handle, index, buffer, length + 1):
            paths.append(buffer.value)
    return paths


def _set_text_locked(text: str) -> None:
    payload = text.encode("utf-16-le") + b"\x00\x00"
    size = len(payload)
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        raise ToolError("为剪贴板分配内存失败。")
    pointer = _kernel32.GlobalLock(handle)
    if not pointer:
        _kernel32.GlobalFree(handle)
        raise ToolError("锁定剪贴板内存失败。")
    try:
        ctypes.memmove(pointer, payload, size)
    finally:
        _kernel32.GlobalUnlock(handle)

    if not _user32.EmptyClipboard():
        _kernel32.GlobalFree(handle)
        raise ToolError(f"清空剪贴板失败（Win32 错误 {ctypes.get_last_error()}）。")
    if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
        error = ctypes.get_last_error()
        _kernel32.GlobalFree(handle)
        raise ToolError(f"写入剪贴板失败（Win32 错误 {error}）。")
    # Success: the clipboard now owns the memory, freeing it here would corrupt it.


# ----------------------------------------------------------------- tool bodies
def read_clipboard(max_chars: int = 8000) -> str:
    """Read the current clipboard content as text.

    Args:
        max_chars: Truncate the returned text to this many characters.
    """

    if not isinstance(max_chars, int) or isinstance(max_chars, bool):
        raise ToolError(f"read_clipboard 的 max_chars 需要整数，收到 {type(max_chars).__name__}。")

    with _ClipboardLock():
        text = _clipboard_text_locked()
        formats = _formats_locked()
        files = _clipboard_files_locked()

    fmt_line = "剪贴板格式：" + (", ".join(formats) if formats else "（空）")

    if text:
        body = text if len(text) <= max_chars else text[:max_chars] + "\n…（已截断）"
        head = f"剪贴板文本（{len(text)} 字符）："
        extra = f"\n\n剪贴板里的文件（{len(files)} 个）：\n" + "\n".join(files) if files else ""
        return f"{head}\n```\n{body}\n```\n{fmt_line}{extra}"

    if files:
        return (
            f"剪贴板是 {len(files)} 个文件/文件夹（没有文本）：\n"
            + "\n".join(f"  {path}" for path in files)
            + f"\n{fmt_line}"
        )

    if formats:
        return f"剪贴板没有文本内容（当前格式：{', '.join(formats)}）。{fmt_line}"
    return "剪贴板是空的。"


def write_clipboard(text: str, append: bool = False) -> str:
    """Put text on the clipboard, replacing whatever was there.

    Args:
        text: Text to copy. Empty string clears the clipboard.
        append: Append to the existing clipboard text instead of replacing it.
    """

    if not isinstance(text, str):
        # The model can hand us a number for "text"; fail with a readable message
        # instead of letting it die later on ``str.encode``.
        raise ToolError(f"write_clipboard 的 text 需要字符串，收到 {type(text).__name__}：{text!r}。")
    if append and not text:
        raise ToolError("append=true 时 text 不能为空。")

    with _ClipboardLock():
        if not text:
            # Writing an empty string would leave an empty-but-registered format
            # behind, so clear the clipboard outright: "empty text" really means
            # "empty clipboard".
            if not _user32.EmptyClipboard():
                raise ToolError(f"清空剪贴板失败（Win32 错误 {ctypes.get_last_error()}）。")
            return "[完成] 已清空剪贴板。"
        if append:
            current = _clipboard_text_locked()
            text = f"{current}\n{text}" if current.strip() else text
        _set_text_locked(text)

    preview = " ".join(text.split())
    if len(preview) > 80:
        preview = preview[:80] + "…"
    verb = "追加到" if append else "写入"
    return f"[完成] 已{verb}剪贴板（{len(text)} 字符）：{preview}"


def clear_clipboard() -> str:
    """Empty the clipboard."""

    with _ClipboardLock():
        if not _user32.EmptyClipboard():
            raise ToolError(f"清空剪贴板失败（Win32 错误 {ctypes.get_last_error()}）。")
    return "[完成] 已清空剪贴板。"


def clipboard_text(max_chars: int = 200) -> str:
    """Raw clipboard text for the TUI panel (returns '' when unavailable)."""

    try:
        with _ClipboardLock():
            text = _clipboard_text_locked()
    except ToolError:
        return ""
    text = " ".join(text.split())
    return text[:max_chars]


__all__ = [
    "clear_clipboard",
    "clipboard_text",
    "read_clipboard",
    "write_clipboard",
]
