"""Headless smoke test for the TUI: boot, commands, modals, plan view, tasks.

Run with:  uv run python -u tests/smoke_tui.py
"""

from __future__ import annotations

import asyncio
import faulthandler
import sys
import tempfile
from pathlib import Path

from jarvis import textwidth
from jarvis.config import build_default_config, load_config
from jarvis.core.agent import ConfirmRequest
from jarvis.tui.app import JarvisApp
from jarvis.tui.screens import ConfirmScreen, HelpScreen, ModelPickerScreen
from jarvis.tui.widgets import MENU, MENU_CONTENT, CommandMenu

# Dump every thread's stack if the test wedges, so a hang is never a mystery.
# The budget is generous because each command waits for the app to look idle,
# and the sidebar's psutil sampling keeps a worker thread busy on a slow box.
faulthandler.dump_traceback_later(240, exit=True)

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
            # The chat pane is a focusable scroll container, so the caret must be
            # placed in the prompt explicitly or typing goes nowhere.
            check(
                "启动即聚焦输入框",
                getattr(app.focused, "id", None) == "prompt",
                str(getattr(app.focused, "id", None) or type(app.focused).__name__),
            )

            # ------------------------------------------------- layout / sidebars
            # Both sidebars start collapsed: the chat is the reason the window is
            # open, and the menu is a detour.
            menu = app.query_one("#menu")
            side = app.query_one("#side")
            check("左侧命令菜单默认收起", menu.has_class("collapsed"))
            check("右侧系统面板默认收起", side.has_class("hidden"))
            check("收起时菜单只剩一条边栏", not app.query_one("#menu-box").display)
            check("收起时面板不采集", app._panel_data is None)

            app.action_toggle_side()
            await pilot.pause()
            await asyncio.sleep(0.8)  # the sampler runs off the UI thread
            await pilot.pause()
            check("Ctrl+S 展开系统面板", not side.has_class("hidden"))

            # The sidebar must never wrap: a line one cell past the box turns the
            # column layout into the ragged mess this test exists to prevent.
            panel = app.query_one("#syspanel")
            panel_width = panel.content_size.width or 43
            panel_lines = str(panel.content).splitlines()
            widest = max(textwidth.dwidth(line) for line in panel_lines)
            check("侧边栏不换行", widest <= panel_width, f"最宽 {widest} 格 / 面板 {panel_width} 格")
            check("侧边栏含四个分区", {"SYSTEM", "DISK", "TOP 进程", "SESSION"} <= set(panel_lines),
                  ",".join(line for line in panel_lines if line in {"SYSTEM", "DISK", "TOP 进程", "SESSION"}))
            check("展开后采样到真实数据", "%" in str(panel.content))

            app.action_toggle_side()
            await pilot.pause()
            check("Ctrl+S 收得起系统面板", side.has_class("hidden"))

            # ------------------------------------------------------- left menu
            menu_list = app.query_one("#menu-list", CommandMenu)
            app.action_toggle_menu()
            await pilot.pause()
            check("Ctrl+B 展开菜单并聚焦列表", getattr(app.focused, "id", None) == "menu-list")
            check("菜单宽度与常量一致", menu_list.content_size.width == MENU_CONTENT,
                  f"{menu_list.content_size.width} / {MENU_CONTENT}")

            rows = [str(option.prompt) for option in menu_list.options]
            row_width = max(textwidth.dwidth(row) for row in rows)
            check("菜单行不超宽", row_width <= MENU_CONTENT, f"最宽 {row_width} / {MENU_CONTENT}")
            check("每个分区都有标题",
                  sum(1 for row in rows if row.startswith("──")) == len(MENU),
                  f"{sum(1 for row in rows if row.startswith('──'))} / {len(MENU)}")
            check("分区标题不选中", all(
                option.disabled for option in menu_list.options
                if str(option.prompt).startswith("──")
            ))

            commands = {
                menu_list.command_for(option.id) for option in menu_list.options
            } - {None}
            check("每一行都绑定了命令", len(commands) == len(rows) - len(MENU),
                  f"{len(commands)} 个命令 / {len(rows) - len(MENU)} 行")
            known = {"/help", "/clear", "/history", "/reset", "/model", "/provider", "/sys",
                     "/ps", "/clip", "/tools", "/task", "/facts", "/learn", "/theme", "/quit"}
            unknown = {cmd.split()[0] for cmd in commands} - known
            check("菜单命令都存在", not unknown, ",".join(sorted(unknown)))

            first = menu_list.options[1]
            check("按键提示不会被当成命令", menu_list.command_for(first.id) == "/help",
                  f"{first.prompt!r} -> {menu_list.command_for(first.id)!r}")

            await pilot.press("down")
            await pilot.pause()
            check("方向键移动高亮", menu_list.highlighted_option.id != first.id,
                  str(menu_list.highlighted_option.prompt))
            await pilot.press("escape")
            await pilot.pause()
            check("Esc 收起菜单", menu.has_class("collapsed"))
            check("收起后焦点回到输入框", getattr(app.focused, "id", None) == "prompt")

            # Activating a row runs its command and puts the menu away again.
            app.action_toggle_menu()
            await pilot.pause()
            target = next(
                option for option in menu_list.options
                if menu_list.command_for(option.id) == "/tools"
            )
            menu_list.highlighted = menu_list.get_option_index(target.id)
            await pilot.pause()
            before = len(chat.children)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            check("菜单项执行对应命令",
                  len(chat.children) > before and "可用工具" in str(chat.children[-1].content),
                  str(chat.children[-1].content)[:40])
            check("执行后菜单自动收起", menu.has_class("collapsed"))
            check("执行后焦点回到输入框", getattr(app.focused, "id", None) == "prompt")

            # A key-bound row shows "F1" but must run /help, not the literal "F1".
            app.action_toggle_menu()
            await pilot.pause()
            menu_list.highlighted = menu_list.get_option_index(first.id)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            check("按键提示行执行的是命令", isinstance(app.screen, HelpScreen),
                  type(app.screen).__name__)
            await pilot.press("escape")
            await pilot.pause()
            check("帮助关掉后焦点回到输入框", getattr(app.focused, "id", None) == "prompt")

            check("调度器已启动", app.scheduler is not None)
            check("测试用的是临时配置", app.config.path.parent == db.parent)

            await type_command(pilot, "/tools")
            check("/tools 有输出", len(chat.children) >= 2)
            check("网络搜索工具已注册", app.registry.get("web_search") is not None)
            check("网页抓取工具已注册", app.registry.get("fetch_url") is not None)
            check("剪贴板工具已注册", app.registry.get("read_clipboard") is not None)
            check("进程工具已注册", app.registry.get("list_processes") is not None)
            check("进程工具共 4 个",
                  {"list_processes", "process_info", "kill_process", "freeze_process"}
                  <= {t.name for t in app.registry.tools})

            # ------------------------------------------------- clipboard / process
            await type_command(pilot, "/ps")
            last = str(chat.children[-1].content)
            check("/ps 输出进程表", "PID" in last, last.splitlines()[0][:50])

            await type_command(pilot, "/ps mem python")
            last = str(chat.children[-1].content)
            check("/ps 支持排序与过滤", "PID" in last, last.splitlines()[0][:50])

            await type_command(pilot, "/clip")
            last = str(chat.children[-1].content)
            check("/clip 有输出", "剪贴板" in last, last.splitlines()[0][:50])

            await type_command(pilot, "/clip 乱七八糟")
            last = str(chat.children[-1].content)
            check("/clip 未知子命令给用法", "用法" in last, last.splitlines()[0][:50])

            # ------------------------------------------------- model & provider
            # A bare /model opens the arrow-key picker; /model list prints the table.
            await type_command(pilot, "/model list")
            check("/model list 打印模型表", "模型列表" in str(chat.children[-1].content))

            await type_command(pilot, "/model")
            check("/model 打开交互式选择器", isinstance(app.screen, ModelPickerScreen))
            if isinstance(app.screen, ModelPickerScreen):
                listing = app.screen.query_one("#picker-list")
                start = listing.highlighted
                check("选择器默认高亮当前模型",
                      app.screen.choices[start][0] == app.agent.model_key, str(start))
                await pilot.press("down")
                await pilot.pause()
                moved = listing.highlighted
                check("↑↓ 能移动高亮", moved == start + 1, f"{start} -> {moved}")
                target = app.screen.choices[moved][0]
                keep = app.agent.model_key
                await pilot.press("escape")
                await pilot.pause()
                await pilot.pause()
                check("Esc 关掉选择器且不切换",
                      not isinstance(app.screen, ModelPickerScreen) and app.agent.model_key == keep)
                check("关掉选择器后焦点回到输入框",
                      getattr(app.focused, "id", None) == "prompt",
                      str(getattr(app.focused, "id", None)))

                await type_command(pilot, "/model")
                await pilot.press("down")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                await pilot.pause()
                check("Enter 按高亮项切换模型", app.agent.model_key == target,
                      f"{app.agent.model_key} / 期望 {target}")
                check("选择器用完自动关闭", not isinstance(app.screen, ModelPickerScreen))

            # Park on the first model so "/model 2" is guaranteed to be a change.
            await type_command(pilot, "/model 1")
            before = app.agent.model_key
            await type_command(pilot, "/model 2")
            switched = app.agent.model_key
            check("按序号热切换模型", switched != before and switched in app.config.model_names())
            check("切换后上下文保留", app.agent.messages[0]["role"] == "system")
            check("切换同时写入默认模型", app.config.default_model == switched, app.config.default_model)
            check("默认模型已落盘",
                  f'default_model = "{switched}"' in app.config.path.read_text(encoding="utf-8"))

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

            # Both sidebars are covered above (menu and panel open/close); the
            # panel is closed there so the rest of the run stays cheap.

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
