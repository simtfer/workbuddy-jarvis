"""Resident daemon: global hotkey + scheduled tasks, without a TUI attached."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR, Config
from ..core.agent import Agent
from ..core.scheduler import ScheduledTask, Scheduler, TaskStore
from ..memory.store import HistoryStore, default_db_path
from ..tools import build_registry, sysinfo
from ..tools import notify as notify_mod
from .hotkey import GlobalHotkey, describe
from .tray import TrayIcon, describe_state

PID_FILE = DATA_DIR / "jarvis.pid"
LOG_FILE = DATA_DIR / "daemon.log"


def log(message: str) -> None:
    """Append one timestamped line to data/daemon.log (and echo to stdout)."""

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------- pidfile
def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:  # noqa: BLE001
        return False


def already_running() -> int | None:
    pid = _read_pid()
    if pid and pid != os.getpid() and _alive(pid):
        return pid
    return None


def write_pid() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def clear_pid() -> None:
    try:
        PID_FILE.unlink()
    except OSError:
        pass


# ------------------------------------------------------------------------ tasks
def build_task_agent(config: Config, store: HistoryStore, task: ScheduledTask) -> Agent:
    """An agent for unattended runs: dangerous tools only if the task opted in."""

    registry = build_registry(config.security, str(config.workdir), config.search)

    async def handler(request):  # noqa: ANN001, ANN202
        if task.allow_dangerous:
            log(f"  自动同意危险操作：{request.summary}")
            return True
        log(f"  已拒绝危险操作（任务未开启 --danger）：{request.summary}")
        return False

    return Agent(
        config=config,
        registry=registry,
        model_name=config.default_model,
        confirm_handler=handler,
        session_id=f"task-{task.id}",
        facts_provider=lambda: [text for _id, text in store.facts(config.memory.max_facts_in_prompt)],
    )


async def run_task(config: Config, store: HistoryStore, task: ScheduledTask) -> str:
    """Execute one scheduled task and return the assistant's answer."""

    agent = build_task_agent(config, store, task)
    chunks: list[str] = []
    try:
        async for event in agent.run(task.prompt):
            if event["type"] == "text":
                chunks.append(event["text"])
            elif event["type"] == "error":
                chunks.append(f"\n[错误] {event['message']}")
            elif event["type"] == "tool_start":
                log(f"  工具 {event['name']} {event.get('arguments')}")
    finally:
        await agent.aclose()

    answer = "".join(chunks).strip() or "(无输出)"
    store.add_message(f"task-{task.id}", "user", task.prompt)
    store.add_message(f"task-{task.id}", "assistant", answer)
    return answer


# ------------------------------------------------------------------------ run
def spawn_tui() -> subprocess.Popen | None:
    """Open a fresh JARVIS window (new console, scheduler disabled - the daemon owns it)."""

    command = [sys.executable, "-m", "jarvis", "--no-scheduler"]
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        return subprocess.Popen(command, creationflags=flags, cwd=str(Path.cwd()))
    except Exception as exc:  # noqa: BLE001
        log(f"唤起 TUI 失败：{exc}")
        return None


async def run_daemon(
    config: Config,
    *,
    hotkey: str | None = None,
    with_scheduler: bool = True,
    open_tui: bool = True,
    tray: bool = True,
) -> int:
    """Block until Ctrl+C or the tray's 退出, serving hotkey + scheduler + tray."""

    running = already_running()
    if running:
        log(f"已有 JARVIS 守护进程在运行（pid={running}），本次退出。")
        return 1

    write_pid()
    store = HistoryStore(default_db_path())
    hotkey_spec = hotkey or config.daemon.hotkey
    listener: GlobalHotkey | None = None
    scheduler: Scheduler | None = None
    tray_icon: TrayIcon | None = None
    task_store = TaskStore(store.conn)
    stop_event = asyncio.Event()

    def open_window(why: str) -> None:
        log(f"{why} → 拉起 JARVIS 窗口")
        if open_tui:
            process = spawn_tui()
            log(f"  已拉起 TUI 窗口（pid={process.pid if process else '失败'}）")

    def on_hotkey() -> None:
        open_window(f"热键 {describe(hotkey_spec)} 触发")

    def on_tray_open() -> None:
        open_window("托盘：打开 JARVIS")

    def on_tray_tasks() -> None:
        tasks = [t for t in task_store.list() if t.enabled]
        if not tasks:
            tell("JARVIS · 定时任务", "当前没有启用中的定时任务")
            return
        upcoming = sorted(tasks, key=lambda t: t.next_run or "9999")
        lines = [
            f"#{t.id} {t.next_run[5:16] if t.next_run else '-'} {t.title[:26]}"
            for t in upcoming[:5]
        ]
        tell(f"JARVIS · {len(tasks)} 个定时任务", "\n".join(lines))
        log(f"托盘：查看任务（{len(tasks)} 个启用）")

    def on_tray_status() -> None:
        try:
            data = sysinfo.snapshot(include_processes=0)
            model = config.model(config.default_model)
            text = (
                f"CPU {data['cpu']} · MEM {data['memory']}\n"
                f"模型 {model.display} · 搜索 {config.search.provider}\n"
                f"热键 {describe(hotkey_spec)} · pid {os.getpid()}"
            )
        except Exception as exc:  # noqa: BLE001
            text = f"状态采集失败：{exc}"
        tell("JARVIS · 状态", text)

    def on_tray_quit() -> None:
        log("托盘：退出")
        loop.call_soon_threadsafe(stop_event.set)

    def tell(title: str, message: str) -> None:
        """Prefer the tray balloon (no subprocess); fall back to a Windows toast."""

        if tray_icon is not None and tray_icon.available and tray_icon.notify(title, message):
            return
        if config.daemon.notify:
            notify_mod.notify(title, message)

    loop = asyncio.get_running_loop()

    try:
        listener = GlobalHotkey(hotkey_spec, on_hotkey).start()
        if listener.error:
            log(f"热键不可用：{listener.error}")
        else:
            log(f"热键就绪：{describe(hotkey_spec)}")

        if with_scheduler:
            scheduler = Scheduler(task_store, runner=_make_runner(config, store))
            scheduler.on_error = lambda task, err: log(f"任务#{task.id} 出错：{err}")
            scheduler.start()
            pending = [t for t in task_store.list() if t.enabled]
            log(f"调度器已启动，共 {len(pending)} 个启用任务，每 {scheduler.check_interval:.0f}s 检查一次")

        if tray and config.daemon.tray:
            tray_icon = TrayIcon(
                tooltip=config.daemon.tray_tooltip,
                on_open=on_tray_open,
                on_tasks=on_tray_tasks,
                on_status=on_tray_status,
                on_quit=on_tray_quit,
            ).start()
            log(describe_state(tray_icon))
        else:
            log("托盘图标：已按要求关闭")

        log(f"守护进程 pid={os.getpid()}，Ctrl+C 或托盘「退出」结束")
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if scheduler is not None:
            await scheduler.stop()
        if listener is not None:
            listener.stop()
        if tray_icon is not None:
            tray_icon.stop()
        store.close()
        if config.daemon.notify and sys.platform == "win32":
            await asyncio.to_thread(notify_mod.notify, "JARVIS", "守护进程已退出")
        clear_pid()
        log("守护进程已退出")
    return 0


def _make_runner(config: Config, store: HistoryStore):
    async def runner(task: ScheduledTask) -> None:
        log(f"任务#{task.id} 触发：{task.prompt[:100]}（{task.schedule_text}）")
        answer = await run_task(config, store, task)
        summary = " ".join(answer.split())[:180] or "(无输出)"
        log(f"任务#{task.id} 完成：{summary}")
        if config.daemon.notify:
            await asyncio.to_thread(
                notify_mod.notify, f"JARVIS · {task.title[:40]}", summary
            )

    return runner
