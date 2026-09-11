"""System information tool backed by psutil."""

from __future__ import annotations

import platform
import socket
import time
from datetime import datetime, timedelta

import psutil


def _bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def snapshot(include_processes: int = 8) -> dict[str, str]:
    """Collect a live snapshot of the machine (used by the TUI panel)."""

    # interval=None never blocks: usage is measured since the previous call,
    # which is exactly what a refresh-every-2s panel wants.
    cpu_percent = psutil.cpu_percent(interval=None)
    per_cpu = psutil.cpu_percent(interval=None, percpu=True)
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    boot = datetime.fromtimestamp(psutil.boot_time())
    uptime = timedelta(seconds=time.time() - psutil.boot_time())

    disks = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        disks.append((part.device, usage))

    net = psutil.net_io_counters()
    battery_text = "无电池（台式机）"
    try:
        battery = psutil.sensors_battery()
    except (AttributeError, NotImplementedError):
        battery = None
    if battery is not None:
        state = "充电中" if battery.power_plugged else "放电中"
        battery_text = f"{battery.percent:.0f}% · {state}"

    processes: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        try:
            processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    processes.sort(
        key=lambda p: (p.info.get("cpu_percent") or 0, (p.info.get("memory_info").rss if p.info.get("memory_info") else 0)),
        reverse=True,
    )
    top = processes[:include_processes]

    return {
        "host": f"{socket.gethostname()} · {platform.system()} {platform.release()}",
        "cpu": f"{cpu_percent:.1f}% · {psutil.cpu_count(logical=True)} 逻辑核",
        "per_cpu": " ".join(f"{v:.0f}%" for v in per_cpu),
        "memory": f"{memory.percent:.1f}% · {_bytes(memory.used)} / {_bytes(memory.total)}",
        "swap": f"{swap.percent:.1f}% · {_bytes(swap.used)} / {_bytes(swap.total)}",
        "boot": f"{boot:%Y-%m-%d %H:%M} · 已运行 {str(uptime).split('.')[0]}",
        "battery": battery_text,
        "net": f"↓{_bytes(net.bytes_recv)} ↑{_bytes(net.bytes_sent)} · 包 {net.packets_recv:,}/{net.packets_sent:,}",
        "disks": "\n".join(
            f"  {device}: {usage.percent:.0f}% 已用 · {_bytes(usage.free)} 可用 / {_bytes(usage.total)}"
            for device, usage in disks
        ) or "  (无)",
        "top": "\n".join(
            f"  {proc.info['name'][:28]:<28} CPU {proc.info.get('cpu_percent') or 0:>5.1f}%  "
            f"MEM {_bytes(proc.info['memory_info'].rss) if proc.info.get('memory_info') else 'n/a'}"
            for proc in top
        ),
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
