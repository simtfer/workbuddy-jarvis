"""Daemon check: starts the real daemon, verifies hotkey/scheduler/pid handling.

Run with:  uv run python -u tests/daemon_check.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "daemon.log"
PID = ROOT / "data" / "jarvis.pid"

FAILURES: list[str] = []


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001
        return False


def _kill(pid: int) -> None:
    if pid <= 0:
        return
    try:
        import psutil

        for child in psutil.Process(pid).children(recursive=True):
            child.kill()
        psutil.Process(pid).kill()
    except Exception:  # noqa: BLE001
        pass


def press_hotkey() -> None:
    """Inject a real Ctrl+Alt+J keypress, like the user would."""

    import ctypes

    VK_CONTROL, VK_MENU, VK_J, KEYUP = 0x11, 0x12, 0x4A, 0x0002
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_J, 0, 0, 0)
    user32.keybd_event(VK_J, 0, KEYUP, 0)
    user32.keybd_event(VK_MENU, 0, KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  ({detail})" if detail else ""), flush=True)
    if not condition:
        FAILURES.append(label)


def read_log() -> str:
    try:
        return LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    PID.unlink(missing_ok=True)
    LOG.unlink(missing_ok=True)

    daemon = subprocess.Popen(
        [sys.executable, "-m", "jarvis", "--daemon", "--no-open-tui"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logged_pid = 0
    try:
        ready = wait_for(lambda: "守护进程 pid=" in read_log())
        check("守护进程启动", ready and daemon.poll() is None, f"launcher={daemon.pid}")

        log = read_log()
        hotkey_line = next((line for line in log.splitlines() if "热键" in line), "(无热键日志)")
        check("热键已注册或以明确原因失败", "热键就绪" in log or "热键不可用" in log, hotkey_line)
        check("调度器已启动", "调度器已启动" in log)

        # On Windows the venv python launcher spawns a child, so the pid that
        # matters is the one the daemon reports about itself.
        match = re.search(r"守护进程 pid=(\d+)", log)
        logged_pid = int(match.group(1)) if match else 0
        pid_file_text = PID.read_text(encoding="utf-8").strip() if PID.exists() else ""
        check("pid 文件写入正确", logged_pid > 0 and pid_file_text == str(logged_pid),
              f"file={pid_file_text} daemon={logged_pid}")

        # A second daemon must refuse to start.
        second = subprocess.run(
            [sys.executable, "-m", "jarvis", "--daemon", "--no-open-tui"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=90,
        )
        refused = "已有 JARVIS 守护进程在运行" in read_log()
        check("重复启动被拒绝", second.returncode == 1 and refused, f"rc={second.returncode}")

        # Try a real keypress. Injected keys are swallowed in some sandboxed
        # sessions, so a miss is reported as a warning, not a failure - the
        # message-loop wiring itself is covered by tests/phase2.py.
        press_hotkey()
        fired = wait_for(lambda: "触发" in read_log(), timeout=15.0)
        if fired:
            trigger_line = next(
                (line for line in read_log().splitlines() if "触发" in line), ""
            )
            check("真实按键触发全局热键", True, trigger_line)
        else:
            print("[warn] 注入按键未触发热键（当前会话禁用了模拟输入）；"
                  "请手动按 Ctrl+Alt+J 验证，消息循环本身已在 phase2.py 覆盖", flush=True)

        check("守护进程仍在运行", _alive(logged_pid))
    finally:
        _kill(daemon.pid)
        _kill(logged_pid)
        try:
            daemon.wait(timeout=15)
        except subprocess.TimeoutExpired:
            daemon.kill()

    time.sleep(1.0)
    check("进程已退出", not _alive(logged_pid))

    from jarvis.daemon.service import already_running

    check("残留 pid 不会误判为运行中", already_running() is None)

    if FAILURES:
        print(f"\nDAEMON CHECK FAILED: {FAILURES}")
        return 1
    print("\nDAEMON CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
