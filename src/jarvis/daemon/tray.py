"""System tray icon for the resident daemon - pure ctypes, no new deps.

Creates a single invisible top-level window inside its own thread, registers a
``Shell_NotifyIconW`` icon with it and runs a classic ``GetMessageW`` loop.
Right-clicking the icon opens a popup menu (打开 JARVIS / 定时任务 / 状态 /
退出); left-clicking opens a JARVIS window.

Everything is best-effort: if the shell refuses the icon (locked-down session,
no explorer.exe) the daemon keeps running and ``error`` explains why.
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable

IS_WINDOWS = os.name == "nt"

# ----------------------------------------------------------------- constants
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD = 0x0000
NIM_MODIFY = 0x0001
NIM_DELETE = 0x0002
NIM_SETVERSION = 0x0004

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_STATE = 0x00000008
NIF_INFO = 0x00000010
NIF_SHOWTIP = 0x00000080

NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002
NIIF_ERROR = 0x00000003

NOTIFYICON_VERSION_4 = 4

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_GRAYED = 0x00000001

TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080
TPM_RIGHTBUTTON = 0x0002

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
IDI_APPLICATION = 32512

MENU_OPEN = 1001
MENU_TASKS = 1002
MENU_STATUS = 1003
MENU_QUIT = 1004

ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "jarvis.ico"


# ------------------------------------------------------------------- structs
if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - the project targets Windows
    user32 = shell32 = kernel32 = None

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


def _bind() -> None:
    """Set argtypes/restypes once; without them 64-bit handles get truncated."""

    if not IS_WINDOWS:
        return
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEX)]
    user32.RegisterClassExW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
    ]
    user32.GetMessageW.restype = ctypes.c_int
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.LoadImageW.restype = wintypes.HICON
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.LoadIconW.restype = wintypes.HICON
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [
        wintypes.HMENU, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR
    ]
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
    ]
    user32.TrackPopupMenu.restype = wintypes.BOOL
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    shell32.Shell_NotifyIconW.argtypes = [
        wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)
    ]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE


if IS_WINDOWS:
    _bind()


# --------------------------------------------------------------------- class
class TrayIcon:
    """A tray icon plus its popup menu, alive on its own thread."""

    def __init__(
        self,
        *,
        tooltip: str = "JARVIS-Win",
        icon_path: Path | str | None = None,
        on_open: Callable[[], None] | None = None,
        on_tasks: Callable[[], None] | None = None,
        on_status: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        menu_labels: dict[str, str] | None = None,
    ) -> None:
        self.tooltip = tooltip[:127]
        self.icon_path = Path(icon_path) if icon_path else ICON_PATH
        self.on_open = on_open
        self.on_tasks = on_tasks
        self.on_status = on_status
        self.on_quit = on_quit
        labels = {"open": "打开 JARVIS", "tasks": "定时任务", "status": "状态", "quit": "退出"}
        labels.update(menu_labels or {})
        self.menu_labels = labels

        self.hwnd: int | None = None
        self.added = False
        self.error: str | None = None
        self.last_error: str | None = None
        self.clicks = 0

        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._wndproc = None  # keep the callback alive or ctypes crashes
        self._icon_handle: int | None = None
        self._owns_icon = False
        self._class_name = f"JARVIS_Tray_{os.getpid()}"
        self._cb_size: int | None = None
        self._taskbar_msg = 0

    # ------------------------------------------------------------------ control
    @property
    def available(self) -> bool:
        return IS_WINDOWS and self.added and self.error is None

    def start(self, timeout: float = 5.0) -> "TrayIcon":
        if not IS_WINDOWS:
            self.error = "托盘图标仅在 Windows 上可用"
            self._ready.set()
            return self
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._run, name="jarvis-tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self

    def stop(self) -> None:
        hwnd = self.hwnd
        if IS_WINDOWS and hwnd:
            try:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None
        self.added = False
        self.hwnd = None

    # ------------------------------------------------------------------- notify
    def notify(self, title: str, message: str, *, warning: bool = False) -> bool:
        """Show a balloon/toast attached to the tray icon."""

        if not self.available or not self.hwnd:
            return False
        data = self._base_data()
        data.uFlags = NIF_INFO
        data.szInfoTitle = title[:63]
        data.szInfo = " ".join(str(message).split())[:255]
        data.uVersion = 10000
        data.dwInfoFlags = NIIF_WARNING if warning else NIIF_INFO
        return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)))

    # --------------------------------------------------------------------- loop
    def _run(self) -> None:
        try:
            self._create_window()
        except Exception as exc:  # noqa: BLE001 - never kill the daemon
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._ready.set()

        if self.error or not self.hwnd:
            return

        message = wintypes.MSG()
        try:
            while True:
                outcome = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if outcome in (0, -1):
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            self._remove_icon()
            if self._icon_handle and self._owns_icon:
                user32.DestroyIcon(self._icon_handle)
            self._icon_handle = None
            self.added = False

    # ------------------------------------------------------------- window setup
    def _create_window(self) -> None:
        hinstance = kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._wnd_proc)

        wc = WNDCLASSEX()
        wc.cbSize = ctypes.sizeof(WNDCLASSEX)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinstance
        wc.lpszClassName = self._class_name

        if not user32.RegisterClassExW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):  # 1410 = class already registered
                raise OSError(f"RegisterClassExW 失败（错误码 {err}）")

        self.hwnd = user32.CreateWindowExW(
            0, self._class_name, "JARVIS", 0, 0, 0, 0, 0, None, None, hinstance, None
        )
        if not self.hwnd:
            raise OSError(f"CreateWindowExW 失败（错误码 {ctypes.get_last_error()}）")

        self._taskbar_msg = user32.RegisterWindowMessageW("TaskbarCreated")
        self._icon_handle = self._load_icon()
        self._add_icon()

    def _load_icon(self) -> int:
        if self.icon_path.exists():
            handle = user32.LoadImageW(
                None,
                str(self.icon_path),
                IMAGE_ICON,
                0,
                0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE,
            )
            if handle:
                self._owns_icon = True
                return handle
        return user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _base_data(self) -> NOTIFYICONDATA:
        data = NOTIFYICONDATA()
        data.cbSize = self._cb_size or ctypes.sizeof(NOTIFYICONDATA)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._icon_handle
        data.szTip = self.tooltip
        return data

    def _delete_icon(self) -> None:
        """Best-effort NIM_DELETE. Safe even when nothing is registered."""

        if not IS_WINDOWS:
            return
        try:
            data = self._base_data()
            data.uFlags = 0
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        except Exception:  # noqa: BLE001
            pass

    def _add_icon(self) -> None:
        # Drop any stale registration first. Re-adding on top of a live icon
        # makes Shell_NotifyIconW fail with E_FAIL, which used to be reported
        # as "tray unavailable" even though the icon was working fine.
        self._delete_icon()

        candidates = [
            ctypes.sizeof(NOTIFYICONDATA),
            NOTIFYICONDATA.hBalloonIcon.offset,
            NOTIFYICONDATA.guidItem.offset,
        ]
        last = 0
        for size in candidates:
            data = self._base_data()
            data.cbSize = size
            data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
            if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
                self._cb_size = size
                self.added = True
                self.error = None  # recovered from any earlier failure
                # Ask for version 4 so left-click / right-click behave as on Win10+.
                version = self._base_data()
                version.uFlags = 0
                version.uVersion = NOTIFYICON_VERSION_4
                shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(version))
                return
            last = ctypes.get_last_error()
        self.error = f"Shell_NotifyIconW(NIM_ADD) 失败（错误码 {last}）"

    def _remove_icon(self) -> None:
        if not self.added:
            return
        self._delete_icon()
        self.added = False

    # ----------------------------------------------------------------- procedures
    def _wnd_proc(self, hwnd, msg, wparam, lparam):  # noqa: ANN001
        try:
            if msg == self._taskbar_msg and self._taskbar_msg:
                # explorer.exe restarted - the icon is gone, put it back.
                self._add_icon()
                return 0
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.clicks += 1
                    self._fire(self.on_open)
                elif event == WM_RBUTTONUP:
                    self._show_menu()
                return 0
            if msg == WM_COMMAND:
                self._dispatch(wparam & 0xFFFF)
                return 0
            if msg == WM_CLOSE:
                self._remove_icon()
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception as exc:  # noqa: BLE001 - a broken callback must not kill the loop
            self.last_error = f"{type(exc).__name__}: {exc}"
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _fire(self, callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"

    def _show_menu(self) -> None:
        entries: list[tuple[int, str, Callable[[], None] | None]] = [
            (MENU_OPEN, self.menu_labels["open"], self.on_open),
            (MENU_TASKS, self.menu_labels["tasks"], self.on_tasks),
            (MENU_STATUS, self.menu_labels["status"], self.on_status),
            (0, "", None),  # separator
            (MENU_QUIT, self.menu_labels["quit"], self.on_quit),
        ]
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        for ident, label, callback in entries:
            if not label:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                continue
            flags = MF_STRING | (0 if callback is not None else MF_GRAYED)
            user32.AppendMenuW(menu, flags, ident, label)

        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # TrackPopupMenu only dismisses properly with a foreground owner window.
        user32.SetForegroundWindow(self.hwnd)
        choice = user32.TrackPopupMenu(
            menu,
            TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
            point.x,
            point.y,
            0,
            self.hwnd,
            None,
        )
        user32.DestroyMenu(menu)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        if choice:
            self._dispatch(int(choice))

    def _dispatch(self, ident: int) -> None:
        mapping = {
            MENU_OPEN: self.on_open,
            MENU_TASKS: self.on_tasks,
            MENU_STATUS: self.on_status,
            MENU_QUIT: self.on_quit,
        }
        self._fire(mapping.get(int(ident)))


def describe_state(tray: TrayIcon | None) -> str:
    if tray is None:
        return "托盘图标：未启用"
    if tray.error:
        return f"托盘图标：不可用（{tray.error}）"
    if tray.added:
        return "托盘图标：已就绪（右键菜单：打开 JARVIS / 定时任务 / 状态 / 退出）"
    return "托盘图标：未就绪"
