"""Process management tools backed by psutil.

Four tools live here:

* ``list_processes``  — a Task-Manager-like table (CPU / memory / user).
* ``process_info``    — deep detail for one process (command line, ports, children…).
* ``kill_process``    — end a process, with a hard guard against Windows' own
  critical processes and against killing JARVIS itself.
* ``freeze_process``  — suspend a CPU hog without losing its state, and thaw it again.

The safety net is deliberately below the model: a process name on the protected
list is refused no matter how the request is phrased.
"""

from __future__ import annotations

import functools
import os
import time
from datetime import datetime, timedelta
from typing import Any, Callable

import psutil

from ..core.registry import ToolError
from .sysinfo import psutil_guard


def _serialized(func: Callable[..., Any]) -> Callable[..., Any]:
    """Run a psutil-touching call under the shared lock.

    Two threads inside psutil's Windows C extension at once deadlock, and the
    UI thread samples processes every 2 seconds — see ``sysinfo.PSUTIL_LOCK``.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with psutil_guard():
            return func(*args, **kwargs)

    return wrapper


# Losing any of these makes Windows itself unstable (or bluescreen outright),
# so they are refused even though the caller confirmed the action.
PROTECTED_NAMES = {
    "system",
    "registry",
    "memory compression",
    "idle",
    "secure system",
    "smss",
    "smss.exe",
    "csrss",
    "csrss.exe",
    "wininit",
    "wininit.exe",
    "services",
    "services.exe",
    "lsass",
    "lsass.exe",
    "winlogon",
    "winlogon.exe",
    "fontdrvhost",
    "fontdrvhost.exe",
}


def _bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _duration(seconds: float) -> str:
    return str(timedelta(seconds=max(0, int(seconds))))


def _name_of(proc: psutil.Process) -> str:
    try:
        return proc.name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return "(未知)"


@_serialized
def _lineage() -> set[int]:
    """PID of this process plus every ancestor — killing one kills JARVIS too."""

    pids: set[int] = set()
    try:
        proc: psutil.Process | None = psutil.Process(os.getpid())
    except psutil.Error:
        return pids
    while proc is not None:
        try:
            pids.add(proc.pid)
            proc = proc.parent()
        except psutil.Error:
            break
    return pids


def _assert_killable(proc: psutil.Process, name: str) -> None:
    if proc.pid <= 4:
        raise ToolError(f"拒绝结束 PID {proc.pid}（{name}）——这是 Windows 核心系统进程。")
    lowered = name.lower()
    if lowered in PROTECTED_NAMES:
        raise ToolError(
            f"拒绝结束 {name} —— 它是系统的关键进程，强行停掉会导致系统不稳定甚至蓝屏。"
        )
    if proc.pid == os.getpid():
        raise ToolError("拒绝结束 JARVIS 自己。")
    if proc.pid in _lineage():
        raise ToolError(
            f"拒绝结束 PID {proc.pid}（{name}）——它属于 JARVIS 所在的进程链，"
            "停掉会连带中断当前会话。"
        )


# ------------------------------------------------------------------- snapshot
# ``cpu_percent`` needs a starting point, so the counters are primed and read
# again after this window. A quarter of a second ranks processes fine without
# making the caller wait a full second.
CPU_WINDOW = 0.25

# One full pass costs ~1s per attribute on a machine with a few hundred
# processes (``status`` alone was measured at 1.7s), and ``cpu_percent`` needs
# two passes. So the list is built from cheap fields only, and the expensive
# ones (memory, status, user) are fetched just for the rows that get printed.
_SORTS = {
    "cpu": lambda row: (-row["cpu"], -row["rss"]),
    "memory": lambda row: (-row["rss"], -row["cpu"]),
    "mem": lambda row: (-row["rss"], -row["cpu"]),
    "name": lambda row: row["name"].lower(),
    "pid": lambda row: row["pid"],
}


def _rss(proc: psutil.Process) -> int:
    try:
        return proc.memory_info().rss
    except (psutil.Error, OSError):
        return 0


def _status(proc: psutil.Process) -> str:
    try:
        return proc.status()
    except (psutil.Error, OSError):
        return ""


def _username(proc: psutil.Process) -> str:
    try:
        return (proc.username() or "").split("\\")[-1]
    except (psutil.Error, OSError):
        return ""


@_serialized
def _collect(sort_by: str, limit: int, needle: str) -> tuple[int, list[dict[str, Any]]]:
    """Return ``(total_matches, rows_to_show)``.

    CPU values are divided by the logical core count so 100% means "all cores
    saturated", matching what Task Manager shows.
    """

    procs: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = proc.info.get("name") or ""
        except psutil.Error:
            continue
        if needle and needle not in name.lower():
            continue
        procs.append(proc)

    if not procs:
        return 0, []

    for proc in procs:
        try:
            proc.cpu_percent(None)
        except (psutil.Error, OSError):
            continue
    time.sleep(CPU_WINDOW)

    cores = psutil.cpu_count(logical=True) or 1
    rows: list[dict[str, Any]] = []
    for proc in procs:
        try:
            cpu = proc.cpu_percent(None) / cores
            name = proc.info.get("name") or "(未知)"
        except (psutil.Error, OSError):
            continue
        rows.append(
            {"pid": proc.pid, "name": name, "cpu": cpu, "rss": 0, "status": "", "username": ""}
        )

    total = len(rows)
    if not rows:
        return 0, []

    by_memory = sort_by in {"memory", "mem"}
    if by_memory:
        # Ranking by memory is the one case that needs the value for everyone.
        for row in rows:
            try:
                row["rss"] = psutil.Process(row["pid"]).memory_info().rss
            except (psutil.Error, OSError):
                row["rss"] = 0

    rows.sort(key=_SORTS[sort_by])
    shown = rows[:limit]

    for row in shown:
        proc = psutil.Process(row["pid"])
        if not by_memory:
            row["rss"] = _rss(proc)
        row["status"] = _status(proc)
        row["username"] = _username(proc)
    return total, shown


def list_processes(sort_by: str = "cpu", limit: int = 25, name_contains: str = "") -> str:
    """List running processes, most interesting first.

    Args:
        sort_by: ``cpu`` (default), ``memory``, ``name`` or ``pid``.
        limit: How many rows to return (1-200).
        name_contains: Only show processes whose name contains this text.
    """

    key = (sort_by or "cpu").strip().lower()
    if key not in _SORTS:
        raise ToolError(f"不支持的排序 '{sort_by}'，可用：cpu / memory / name / pid")
    limit = max(1, min(int(limit or 25), 200))
    needle = name_contains.strip().lower().removesuffix(".exe")

    total, shown = _collect(key, limit, needle)

    if not shown:
        if needle:
            return f"没有找到匹配 '{name_contains}' 的进程。"
        return "读不到任何进程信息。"

    header = f"{'PID':>7}  {'名称':<28} {'CPU%':>6}  {'内存':>10}  {'状态':<12} 用户"
    lines = [header, "-" * len(header)]
    for row in shown:
        lines.append(
            f"{row['pid']:>7}  {row['name'][:28]:<28} {row['cpu']:>6.1f}  "
            f"{_bytes(row['rss']):>10}  {row['status'][:12]:<12} {row['username']}"
        )
    caption = f"共 {total} 个进程，按 {key} 排序，显示前 {len(shown)} 个。"
    if needle:
        caption = f"名称含 '{name_contains}' 的进程 {total} 个，显示前 {len(shown)} 个。"
    return caption + "\n" + "\n".join(lines)


# ------------------------------------------------------------------- details
@_serialized
def _hits(query: str) -> list[psutil.Process]:
    text = (query or "").strip().strip('"')
    if not text:
        raise ToolError("请给出 PID 或进程名。")
    if text.isdigit():
        pid = int(text)
        if not psutil.pid_exists(pid):
            raise ToolError(f"没有 PID 为 {pid} 的进程。")
        return [psutil.Process(pid)]
    needle = text.lower().removesuffix(".exe")
    found: list[psutil.Process] = []
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except psutil.Error:
            continue
        if needle in name:
            found.append(proc)
    return found


@_serialized
def _detail(proc: psutil.Process) -> str:
    try:
        with proc.oneshot():
            name = proc.name()
            status = proc.status()
            created = proc.create_time()
            cpu = proc.cpu_percent(None) / (psutil.cpu_count(logical=True) or 1)
            memory = proc.memory_info()
            mem_percent = proc.memory_percent()
            threads = proc.num_threads()
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        raise ToolError(f"读不到进程信息：{exc}") from None

    def safe(func, default: str) -> Any:
        try:
            return func()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            return default

    started = datetime.fromtimestamp(created)
    uptime = _duration(time.time() - created)
    parent = safe(proc.parent, None)
    parent_text = f"{parent.pid} {_name_of(parent)}" if parent is not None else "(无)"

    lines = [
        f"PID     : {proc.pid}",
        f"名称    : {name}",
        f"状态    : {status}",
        f"用户    : {safe(proc.username, '(未知)')}",
        f"启动    : {started:%Y-%m-%d %H:%M:%S} · 已运行 {uptime}",
        f"CPU     : {cpu:.1f}%（占整机）",
        f"内存    : {_bytes(memory.rss)} 常驻 / {_bytes(memory.vms)} 虚拟 · 占整机 {mem_percent:.1f}%",
        f"线程    : {threads}",
        f"父进程  : {parent_text}",
        f"可执行  : {safe(proc.exe, '(不可读，可能需要管理员权限)')}",
        f"工作目录: {safe(proc.cwd, '(不可读)')}",
    ]

    cmdline = safe(proc.cmdline, [])
    lines.append("命令行  : " + (" ".join(cmdline) if cmdline else "(不可读)"))

    children = safe(lambda: proc.children(recursive=False), [])
    if children:
        lines.append(
            "子进程  : "
            + ", ".join(f"{child.pid} {_name_of(child)}" for child in children[:12])
            + (f" …共 {len(children)} 个" if len(children) > 12 else "")
        )

    connections = safe(lambda: proc.net_connections(kind="inet"), [])
    if connections:
        listed: list[str] = []
        for conn in connections[:8]:
            local = f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else "-"
            remote = f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else "-"
            listed.append(f"{local} → {remote} [{conn.status}]")
        lines.append("网络连接: " + "；".join(listed))
    else:
        lines.append("网络连接: (无或有权限限制)")

    if proc.pid in _lineage():
        lines.append("提示    : 这个进程属于 JARVIS 自己的进程链，不能被 kill_process 结束。")
    return "\n".join(lines)


def process_info(query: str) -> str:
    """Show detail for a process: command line, ports, children, resource use.

    Args:
        query: A PID (``"1234"``) or a name / name fragment (``"chrome"``).
    """

    hits = _hits(query)
    if not hits:
        raise ToolError(f"没有名字里含 '{query}' 的进程。先用 list_processes 看一下。")
    if len(hits) > 1:
        rows = sorted(
            ((proc.pid, _name_of(proc)) for proc in hits), key=lambda item: item[0]
        )
        listing = "\n".join(f"  {pid:>7}  {name}" for pid, name in rows[:30])
        more = f"\n  …共 {len(rows)} 个" if len(rows) > 30 else ""
        return (
            f"'{query}' 匹配到 {len(hits)} 个进程，请用 PID 查看其中一个：\n{listing}{more}"
        )
    return _detail(hits[0])


# ------------------------------------------------------------------ mutations
@_serialized
def kill_process(pid: int, force: bool = False, timeout: float = 3.0) -> str:
    """End a process by PID (graceful first, then forced if ``force`` is true).

    Args:
        pid: The target process id — get it from ``list_processes`` first.
        force: Force-kill when the process ignores a polite termination request.
        timeout: Seconds to wait for the process to exit before reporting back.
    """

    if not psutil.pid_exists(int(pid)):
        raise ToolError(f"没有 PID 为 {pid} 的进程（可能刚才已经退出了）。")
    proc = psutil.Process(int(pid))
    name = _name_of(proc)
    _assert_killable(proc, name)

    try:
        proc.terminate()
    except psutil.AccessDenied as exc:
        raise ToolError(
            f"没有权限结束 {name}（PID {pid}）：{exc}。以管理员身份运行 JARVIS 再试。"
        ) from None

    try:
        proc.wait(timeout=max(0.5, float(timeout)))
        return f"[完成] 已结束 {name}（PID {pid}）。"
    except psutil.TimeoutExpired:
        pass
    except psutil.NoSuchProcess:
        return f"[完成] {name}（PID {pid}）已经退出。"

    if not force:
        return (
            f"[提示] {name}（PID {pid}）没有在 {float(timeout):.0f} 秒内退出"
            "（无窗口的后台进程常见）。要强制结束，再用 force=true 调用一次。"
        )

    try:
        proc.kill()
        proc.wait(timeout=max(0.5, float(timeout)))
    except psutil.AccessDenied as exc:
        raise ToolError(f"强制结束 {name}（PID {pid}）被拒绝：{exc}") from None
    except psutil.NoSuchProcess:
        return f"[完成] {name}（PID {pid}）已经退出。"
    except psutil.TimeoutExpired:
        raise ToolError(f"强制结束 {name}（PID {pid}）后它依然存在，可能需要管理员权限。") from None
    return f"[完成] 已强制结束 {name}（PID {pid}）。"


@_serialized
def freeze_process(pid: int, resume: bool = False) -> str:
    """Suspend a process (freeze it in place) or resume one that was suspended.

    Args:
        pid: Target process id.
        resume: ``true`` to thaw a suspended process instead of freezing it.
    """

    if not psutil.pid_exists(int(pid)):
        raise ToolError(f"没有 PID 为 {pid} 的进程。")
    proc = psutil.Process(int(pid))
    name = _name_of(proc)
    _assert_killable(proc, name)

    try:
        if resume:
            proc.resume()
            return f"[完成] 已恢复 {name}（PID {pid}）。"
        proc.suspend()
        return (
            f"[完成] 已挂起 {name}（PID {pid}）——它不再占用 CPU，但内存仍被占着。"
            "要恢复：freeze_process(pid=..., resume=true)。"
        )
    except psutil.AccessDenied as exc:
        raise ToolError(f"没有权限操作 {name}（PID {pid}）：{exc}") from None
