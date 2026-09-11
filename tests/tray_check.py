"""Real Windows tray-icon check: create it, click it, notify, survive a taskbar restart.

Unlike ``phase3.py`` (which only validates the struct layout offline), this test
talks to the actual shell: ``Shell_NotifyIconW`` at runtime. It needs a desktop
session, so it is kept separate from the offline suite.

Run with:  uv run python -u tests/tray_check.py
"""

from __future__ import annotations

import ctypes
import faulthandler
import sys
import time

from jarvis.daemon import tray as T

faulthandler.dump_traceback_later(60, exit=True)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  ({detail})" if detail else ""), flush=True)
    if not condition:
        FAILURES.append(label)


def main() -> int:
    fired: list[str] = []
    icon = T.TrayIcon(
        tooltip="JARVIS-Win 测试",
        on_open=lambda: fired.append("open"),
        on_tasks=lambda: fired.append("tasks"),
        on_status=lambda: fired.append("status"),
        on_quit=lambda: fired.append("quit"),
    )

    icon.start(timeout=6.0)
    try:
        check("托盘图标已加入通知区", icon.added is True, icon.error or "")
        check("拿到了隐藏窗口句柄", bool(icon.hwnd), hex(icon.hwnd or 0))
        check("回调消息在 WM_APP 区间", T.WM_TRAY >= 0x8000, str(T.WM_TRAY))
        check("注册了 TaskbarCreated 恢复消息", icon._taskbar_msg > 0, str(icon._taskbar_msg))

        # A left click on the icon must reach on_open.
        T.user32.PostMessageW(icon.hwnd, T.WM_TRAY, 1, T.WM_LBUTTONUP)
        time.sleep(0.4)
        check("左键点击回调触发", "open" in fired, str(fired))
        check("点击计数被记录", icon.clicks >= 1, str(icon.clicks))

        # A right-click menu command must reach the matching handler.
        T.user32.PostMessageW(icon.hwnd, T.WM_COMMAND, T.MENU_STATUS, 0)
        time.sleep(0.3)
        check("菜单命令回调触发", "status" in fired, str(fired))

        T.user32.PostMessageW(icon.hwnd, T.WM_COMMAND, T.MENU_TASKS, 0)
        time.sleep(0.3)
        check("任务菜单回调触发", "tasks" in fired, str(fired))

        # An unknown command id must not fire anything.
        before = len(fired)
        T.user32.PostMessageW(icon.hwnd, T.WM_COMMAND, 9999, 0)
        time.sleep(0.2)
        check("未知菜单命令被忽略", len(fired) == before)

        check("气泡通知可用", icon.notify("JARVIS", "托盘自检通知") is True)

        # Explorer restarts broadcast TaskbarCreated; the icon must re-add itself.
        T.user32.PostMessageW(icon.hwnd, icon._taskbar_msg, 0, 0)
        time.sleep(0.5)
        check("任务栏重启后自动重挂", icon.added is True)
        check("重挂后不留下假错误", icon.error is None, str(icon.error))
        check("重挂后仍可用", icon.available is True)

        check("状态文本如实显示已就绪", "已就绪" in T.describe_state(icon), T.describe_state(icon))
    finally:
        icon.stop()
        time.sleep(0.5)

    check("停止后已从通知区移除", icon.added is False)
    check("停止后线程已退出", not icon._thread)
    check("停止时无 Win32 错误", not icon.last_error, str(icon.last_error))

    faulthandler.cancel_dump_traceback_later()
    if FAILURES:
        print(f"\nTRAY CHECK FAILED: {len(FAILURES)} 项未通过 -> {FAILURES}")
        return 1
    print("\nTRAY CHECK PASSED")
    return 0


if __name__ == "__main__":
    if sys.platform != "win32":
        print("skipped: 托盘测试仅支持 Windows")
        raise SystemExit(0)
    ctypes.windll.user32.SetProcessDPIAware()
    raise SystemExit(main())
