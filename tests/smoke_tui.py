"""Headless smoke test for the TUI: boot, commands, modals, plan view, tasks.

Run with:  uv run python -u tests/smoke_tui.py
"""

from __future__ import annotations

import asyncio
import faulthandler
import sys
import tempfile
from pathlib import Path

from jarvis.config import build_default_config, load_config
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


def isolated_config(tmpdir: str):
    """A throwaway config.toml so commands that persist never touch the real one."""

    path = Path(tmpdir) / "config.toml"
    build_default_config().save(path)
    return load_config(path)


async def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db = Path(tmpdir) / "smoke.db"
        app = JarvisApp(isolated_config(tmpdir), db_path=db)

        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause()

            chat = app.query_one("#chat")
            check("应用启动并挂载界面", len(chat.children) >= 1)
            check("系统面板有内容", "CPU" in str(app.query_one("#syspanel").content))
            check("调度器已启动", app.scheduler is not None)
            check("测试用的是临时配置", app.config.path.parent == db.parent)

            await type_command(pilot, "/tools")
            check("/tools 有输出", len(chat.children) >= 2)
            check("网络搜索工具已注册", app.registry.get("web_search") is not None)
            check("网页抓取工具已注册", app.registry.get("fetch_url") is not None)

            # ------------------------------------------------- model & provider
            await type_command(pilot, "/model")
            check("/model 列出模型", len(chat.children) >= 2)

            before = app.agent.model_key
            await type_command(pilot, "/model 2")
            switched = app.agent.model_key
            check("按序号热切换模型", switched != before and switched in app.config.model_names())
            check("切换后上下文保留", app.agent.messages[0]["role"] == "system")

            await type_command(pilot, "/model 不存在")
            check("未知模型被拦住并保持原模型", app.agent.model_key == switched)

            await type_command(pilot, "/provider list")
            check("/provider 列出后端", len(chat.children) >= 2)

            await type_command(
                pilot,
                "/provider add myvllm http://10.0.0.9:8000/v1 --env MYVLLM_KEY --label 内网vLLM",
            )
            check("自定义 provider 写入配置", "myvllm" in app.config.user_providers)
            check("provider 落盘到临时配置", "myvllm" in app.config.path.read_text(encoding="utf-8"))

            await type_command(pilot, "/model add nei --provider myvllm --model qwen3-32b")
            check("自定义模型可用", "nei" in app.config.model_names())
            check("自定义模型继承 base_url", app.config.model("nei").base_url == "http://10.0.0.9:8000/v1")

            await type_command(pilot, "/model nei")
            check("切到内网模型", app.agent.model_key == "nei")
            check("内网模型不算缺 Key", app.config.model("nei").key_ready is True)

            await type_command(pilot, "/model fallback deepseek nei")
            check("fallback 链可写", app.config.fallback_models == ["deepseek", "nei"])

            await type_command(pilot, "/model rm nei")
            check("在用中的模型不能删", "nei" in app.config.model_names())

            await type_command(pilot, "/model deepseek")
            await type_command(pilot, "/model rm nei")
            check("切走后模型可删除", "nei" not in app.config.model_names())

            await type_command(pilot, "/provider rm myvllm")
            check("provider 可删除", "myvllm" not in app.config.user_providers)

            await type_command(pilot, "/search backend searxng")
            check("搜索后端可切换", app.config.search.provider == "searxng")
            check("搜索后端落盘", "searxng" in app.config.path.read_text(encoding="utf-8"))
            await type_command(pilot, "/search backend 不存在的后端")
            check("未知搜索后端被拦住", app.config.search.provider == "searxng")

            await type_command(pilot, "/search")
            check("/search 无参数给用法", len(chat.children) >= 2)
            await type_command(pilot, "/search backend duckduckgo")
            check("搜索后端切回 duckduckgo", app.config.search.provider == "duckduckgo")

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
