"""System information tool backed by psutil."""

from __future__ import annotations

import platform
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator

import psutil

from ..core.registry import ToolError

# psutil's Windows extension is NOT safe under concurrency: when two threads
# call ``process_iter(attrs)`` at the same time they both block forever inside
# the C-level handle cache (reproduced on 3.13 / psutil 7.2.2 — every thread
# parks in ``proc_memory_info``). JARVIS calls psutil from two places that can
# overlap: the 2s system panel and tools running in a worker thread
# (``registry.call`` -> ``asyncio.to_thread``). Every psutil-heavy path takes
# this one lock so they can never run at the same time. It is reentrant because
# tool code occasionally nests snapshots.
PSUTIL_LOCK = threading.RLock()

# If the lock is still held after this long something is wedged inside psutil;
# fail with a readable message instead of blocking a tool call forever.
LOCK_TIMEOUT = 20.0


@contextmanager
def psutil_guard(timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """Serialise access to psutil, degrading to a readable error if wedged."""

    if not PSUTIL_LOCK.acquire(timeout=timeout):
        raise ToolError("系统进程采样还没结束（上一次采样卡住了），等几秒再试一次。")
    try:
        yield
    finally:
        PSUTIL_LOCK.release()


def _bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def snapshot(include_processes: int = 8) -> dict[str, str]:
    """Collect a live snapshot of the machine (used by the TUI panel).

    ``include_processes=0`` skips the process table entirely: that part costs
    seconds on a machine with a few hundred processes (each attribute is a
    separate syscall), while CPU / memory / disk / network are nearly free.
    """

    with psutil_guard():
        return _snapshot_locked(include_processes)


def top_processes(limit: int = 8) -> str:
    """Rows of the busiest processes, highest CPU first (expensive: seconds)."""

    with psutil_guard():
        return _top_processes_locked(limit)


# ``cpu_percent`` reports the delta since the previous call, so the counters are
# primed, given a window to accumulate, then read. 0.25s is enough to rank
# processes without making the caller wait like a full second would.
CPU_WINDOW = 0.25

# Asking Windows about the page file (swap) costs ~0.3s, and disk/boot values
# barely change, so the sidebar reuses them for this long instead of paying that
# price on every 2s refresh.
SLOW_TTL = 60.0
_slow_cache: dict[str, Any] = {}

_SKIP = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError)


def _slow_values() -> tuple[Any, list[Any], datetime, str]:
    """(swap, disks, boot, uptime_text) — cached, because swap_memory() is slow."""

    now = time.time()
    if now - float(_slow_cache.get("at", 0.0)) < SLOW_TTL:
        return (
            _slow_cache["swap"],
            _slow_cache["disks"],
            _slow_cache["boot"],
            _slow_cache["uptime"],
        )

    swap = psutil.swap_memory()
    boot = datetime.fromtimestamp(psutil.boot_time())
    uptime = str(timedelta(seconds=time.time() - psutil.boot_time())).split(".")[0]

    disks = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            disks.append((part.device, psutil.disk_usage(part.mountpoint)))
        except (PermissionError, OSError):
            continue

    _slow_cache.update({"at": now, "swap": swap, "disks": disks, "boot": boot, "uptime": uptime})
    return swap, disks, boot, uptime


def _top_processes_locked(limit: int) -> str:
    """Busiest processes by CPU, memory fetched only for the rows we print."""

    procs: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            proc.info  # noqa: B018 - forces the info lookup to raise early
            procs.append(proc)
        except _SKIP:
            continue
    if not procs:
        return "  (读不到进程)"

    for proc in procs:
        try:
            proc.cpu_percent(None)
        except _SKIP:
            continue
    time.sleep(CPU_WINDOW)

    cores = psutil.cpu_count(logical=True) or 1
    ranked: list[tuple[float, psutil.Process]] = []
    for proc in procs:
        try:
            ranked.append((proc.cpu_percent(None) / cores, proc))
        except _SKIP:
            continue

    ranked.sort(key=lambda item: item[0], reverse=True)
    lines: list[str] = []
    for cpu, proc in ranked[:limit]:
        try:
            rss = proc.memory_info().rss
        except _SKIP:
            rss = 0
        name = proc.info.get("name") or "(未知)"
        lines.append(f"  {name[:28]:<28} CPU {cpu:>5.1f}%  MEM {_bytes(rss)}")
    return "\n".join(lines) or "  (无)"


def _snapshot_locked(include_processes: int) -> dict[str, str]:
    # interval=None never blocks: usage is measured since the previous call,
    # which is exactly what a refresh-every-2s panel wants.
    cpu_percent = psutil.cpu_percent(interval=None)
    per_cpu = psutil.cpu_percent(interval=None, percpu=True)
    memory = psutil.virtual_memory()
    swap, disks, boot, uptime = _slow_values()

    net = psutil.net_io_counters()
    battery_text = "无电池（台式机）"
    try:
        battery = psutil.sensors_battery()
    except (AttributeError, NotImplementedError):
        battery = None
    if battery is not None:
        state = "充电中" if battery.power_plugged else "放电中"
        battery_text = f"{battery.percent:.0f}% · {state}"

    top = _top_processes_locked(include_processes) if include_processes > 0 else "(未采样)"

    return {
        "host": f"{socket.gethostname()} · {platform.system()} {platform.release()}",
        "cpu": f"{cpu_percent:.1f}% · {psutil.cpu_count(logical=True)} 逻辑核",
        "per_cpu": " ".join(f"{v:.0f}%" for v in per_cpu),
        "memory": f"{memory.percent:.1f}% · {_bytes(memory.used)} / {_bytes(memory.total)}",
        "swap": f"{swap.percent:.1f}% · {_bytes(swap.used)} / {_bytes(swap.total)}",
        "boot": f"{boot:%Y-%m-%d %H:%M} · 已运行 {uptime}",
        "battery": battery_text,
        "net": f"↓{_bytes(net.bytes_recv)} ↑{_bytes(net.bytes_sent)} · 包 {net.packets_recv:,}/{net.packets_sent:,}",
        "disks": "\n".join(
            f"  {device}: {usage.percent:.0f}% 已用 · {_bytes(usage.free)} 可用 / {_bytes(usage.total)}"
            for device, usage in disks
        ) or "  (无)",
        "top": top,
    }


def sys_report(include_processes: int = 8) -> str:
    """Return CPU / memory / disk / network / battery status of this Windows machine.

    Args:
        include_processes: How many top processes to list by CPU usage.
    """

    data = snapshot(include_processes)
    return (
        f"主机: {data['host']}\n"
        f"CPU: {data['cpu']}  ({data['per_cpu']})\n"
        f"内存: {data['memory']}\n"
        f"交换: {data['swap']}\n"
        f"启动: {data['boot']}\n"
        f"电池: {data['battery']}\n"
        f"网络: {data['net']}\n"
        f"磁盘:\n{data['disks']}\n"
        f"占资源最多的进程:\n{data['top']}"
    )
