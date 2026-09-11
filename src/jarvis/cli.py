"""Command line entry point: TUI, selftest, daemon."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__
from .config import load_config


def _print_tasks() -> int:
    """List scheduled tasks without starting anything else."""

    from .core.scheduler import TaskStore
    from .memory.store import HistoryStore, default_db_path

    store = HistoryStore(default_db_path())
    try:
        tasks = TaskStore(store.conn).list()
        if not tasks:
            print("没有定时任务。")
            return 0
        for task in tasks:
            state = "启用" if task.enabled else "停用"
            print(
                f"#{task.id:<3} [{state}] {task.schedule_text:<14} "
                f"下次 {task.next_run or '-':<17} {'⚠' if task.allow_dangerous else ' '} {task.prompt}"
            )
    finally:
        store.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS-Win · Windows 超级 AI 助手")
    parser.add_argument("--selftest", action="store_true", help="不开 TUI，自检配置、工具与调度器")
    parser.add_argument("--ping", action="store_true", help="自检时额外发一次真实模型请求")
    parser.add_argument("--daemon", action="store_true", help="常驻模式：全局热键 + 定时任务")
    parser.add_argument("--hotkey", default=None, help="覆盖全局热键，例如 ctrl+alt+j")
    parser.add_argument("--no-scheduler", action="store_true", help="本次运行不启动调度器")
    parser.add_argument("--no-open-tui", action="store_true", help="守护模式下热键不拉起新窗口")
    parser.add_argument("--tasks", action="store_true", help="打印定时任务列表后退出")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    args = parser.parse_args()

    if args.tasks:
        sys.exit(_print_tasks())

    config = load_config()

    if args.selftest or args.ping:
        from .tui.app import _selftest

        sys.exit(_selftest(ping=args.ping))

    if args.daemon:
        from .daemon.service import run_daemon

        print("JARVIS 守护进程启动中…（Ctrl+C 退出）")
        try:
            sys.exit(
                asyncio.run(
                    run_daemon(
                        config,
                        hotkey=args.hotkey,
                        with_scheduler=not args.no_scheduler,
                        open_tui=not args.no_open_tui,
                    )
                )
            )
        except KeyboardInterrupt:
            sys.exit(0)

    from .tui.app import JarvisApp

    app = JarvisApp(config, scheduler_enabled=not args.no_scheduler)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
