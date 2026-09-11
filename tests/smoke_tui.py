"""Headless smoke test for the TUI: boot, commands, modals, plan view, tasks.

Run with:  uv run python -u tests/smoke_tui.py
"""

from __future__ import annotations

import asyncio
import faulthandler
import sys
import tempfile
from pathlib import Path

from jarvis.config import load_config
from jarvis.core.agent import ConfirmRequest
from jarvis.tui.app import JarvisApp
from jarvis.tui.screens import ConfirmScreen

# Dump every thread's stack if the test wedges, so a hang is never a mystery.
faulthandler.dump_traceback_later(60, exit=True)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  ({detail})" if detail else ""), flush=True)
    if not condition:
        FAILURES.append(label)


async def type_command(pilot, command: str) -> None:
    await pilot.press(*command)
    await pilot.press("enter")
    await pilot.pause()
    await pilot.pause()


async def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db = Path(tmpdir) / "smoke.db"
        app = JarvisApp(load_config(), db_path=db)

        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause()

            chat = app.query_one("#chat")
            check("应用启动并挂载界面", len(chat.children) >= 1)
            check("系统面板有内容", "CPU" in str(app.query_one("#syspanel").content))
            check("调度器已启动", app.scheduler is not None)

            await type_command(pilot, "/tools")
            check("/tools 有输出", len(chat.children) >= 2)

            # ---------------------------------------------------------- memory
            await type_command(pilot, "/facts")
            check("/facts 初始为空", app.store.fact_count() == 0)

            await type_command(pilot, "/remember 用户偏好简洁的中文回答")
            check("记忆写入成功", app.store.fact_count() == 1)

            await type_command(pilot, "/remember 用户偏好简洁的中文回答")
            check("重复记忆不重复写入", app.store.fact_count() == 1)

            await type_command(pilot, "/facts")
            fact_id = app.store.facts()[0][0]
            check("记忆注入到系统提示", "简洁的中文回答" in app.agent.messages[0]["content"])

            await type_command(pilot, f"/forget {fact_id}")
            check("记忆删除成功", app.store.fact_count() == 0)

            # ------------------------------------------------------------ tasks
            await type_command(pilot, "/task add 检查磁盘剩余空间 @daily 09:00")
            tasks = app.task_store.list()
            check("定时任务创建成功", len(tasks) == 1 and tasks[0].kind == "daily", str(tasks))
            task_id = tasks[0].id

            await type_command(pilot, "/task list")
            check("/task list 有输出", len(chat.children) >= 2)

            await type_command(pilot, f"/task off {task_id}")
            check("任务可停用", app.task_store.get(task_id).enabled is False)
            await type_command(pilot, f"/task on {task_id}")
            check("任务可启用", app.task_store.get(task_id).enabled is True)

            await type_command(pilot, "/task add 缺少调度表达式的任务")
            check("非法任务被拒绝", len(app.task_store.list()) == 1)

            await type_command(pilot, f"/task rm {task_id}")
            check("任务可删除", app.task_store.list() == [])

            # --------------------------------------------------------- plan view
            await app._handle_event(
                {
                    "type": "plan",
                    "task": "体检",
                    "steps": [
                        {"title": "看磁盘", "detail": "用 sys_report"},
                        {"title": "汇报", "detail": ""},
                    ],
                }
            )
            await pilot.pause()
            plan_view = app._plan_view
            check("计划面板已渲染", plan_view is not None and "看磁盘" in str(plan_view.content))

            await app._handle_event(
                {"type": "step_start", "index": 0, "total": 2, "title": "看磁盘"}
            )
            await pilot.pause()
            check("步骤进入 running", "▶" in str(plan_view.content))

            await app._handle_event({"type": "step_done", "index": 0, "status": "done"})
            await pilot.pause()
            check("步骤进入 done", "✓" in str(plan_view.content))

            # ------------------------------------------------------- modals etc.
            request = ConfirmRequest(tool="run_shell", arguments={"command": "echo hi"}, hint="test")
            answer = asyncio.ensure_future(app._confirm(request))
            await pilot.pause()
            check("确认弹窗弹出", isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")
            check("确认弹窗同意生效", await answer is True)

            answer2 = asyncio.ensure_future(app._confirm(request))
            await pilot.pause()
            await pilot.press("escape")
            check("确认弹窗拒绝生效", await answer2 is False)

            app.action_toggle_side()
            await pilot.pause()
            check("系统面板可隐藏", app.query_one("#side").has_class("hidden"))

            await app.action_clear_chat()
            await pilot.pause()
            check("清屏后计划面板重置", app._plan_view is None)

            await app.action_tasks()
            await pilot.pause()
            check("F2 任务列表可用", len(chat.children) >= 2)

    faulthandler.cancel_dump_traceback_later()
    if FAILURES:
        print(f"\nSMOKE TEST FAILED: {len(FAILURES)} 项未通过 -> {FAILURES}")
        return 1
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
