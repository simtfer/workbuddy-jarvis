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
from jarvis.tui.widgets import MENU, MENU_CONTENT, CommandMenu, ThinkingView, ToolCallView

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

            # ------------------------------------------------- subagent events
            chat = app.query_one("#chat")
            before = len(chat.children)
            await app._handle_event({
                "type": "subagent_start",
                "count": 3,
                "prompts": ["research A", "research B", "research C"],
            })
            await pilot.pause()
            check("subagent_start 渲染派发通知",
                  any("派发了 3 个子任务" in str(getattr(c, "content", "")) for c in chat.children[before:]),
                  str(len(chat.children) - before))

            await app._handle_event({
                "type": "subagent_done",
                "index": 0,
                "status": "done",
                "tool_count": 2,
                "elapsed": 1.5,
            })
            await pilot.pause()
            check("subagent_done 渲染完成标记",
                  any("✓" in str(getattr(c, "content", "")) and "子任务" in str(getattr(c, "content", ""))
                      for c in chat.children))

            await app._handle_event({
                "type": "subagent_timeout",
                "results": [
                    {"index": 0, "prompt": "a", "status": "done"},
                    {"index": 1, "prompt": "b", "status": "running"},
                ],
                "default_timeout": 30.0,
            })
            await pilot.pause()
            check("subagent_timeout 触发部分提示",
                  any("默认超时" in str(getattr(c, "content", "")) for c in chat.children))

            await app._handle_event({
                "type": "subagent_final",
                "results": [
                    {"index": 0, "prompt": "a", "status": "done"},
                    {"index": 1, "prompt": "b", "status": "done"},
                ],
            })
            await pilot.pause()
            check("subagent_final 渲染汇总",
                  any("🏁" in str(getattr(c, "content", "")) and "全部结束" in str(getattr(c, "content", ""))
                      for c in chat.children))

            # --------------------------------------------------- thinking stream
            chat = app.query_one("#chat")
            before = len(chat.children)
            for piece in ("先想想 ", "用户要的是什么", "……有眉目了"):
                await app._handle_event({"type": "thinking", "delta": piece})
            await pilot.pause()
            thinking_view = app._thinking_widget
            check("思考块已渲染", isinstance(thinking_view, ThinkingView))
            check("思考块默认折叠", thinking_view is not None and thinking_view.collapsed is True)
            body = thinking_view.query_one(".thinking-body")
            check("思考内容完整累积", "先想想 用户要的是什么……有眉目了" in str(body.content))
            check("折叠时正文不占聊天区",
                  not any("先想想" in str(getattr(c, "content", "")) for c in chat.children[before:] if not isinstance(c, ThinkingView)))

            await app._handle_event({"type": "text", "text": "答案是 42。"})
            await pilot.pause()
            check("正文开始即封存思考块", app._thinking_widget is None)
            check("思考块标题带字数", "字" in thinking_view.title)

            # A second model round (after a tool call) gets a fresh block.
            await app._handle_event({"type": "tool_start", "name": "echo_test", "arguments": {}})
            await app._handle_event({"type": "thinking", "delta": "再看一眼。"})
            await pilot.pause()
            check("新一轮思考另起新块",
                  isinstance(app._thinking_widget, ThinkingView) and app._thinking_widget is not thinking_view)
            await app._handle_event({"type": "done"})
            await pilot.pause()
            check("done 后思考块封存", app._thinking_widget is None)

            # ------------------------------------------------ tool call blocks
            chat = app.query_one("#chat")
            await app._handle_event(
                {"type": "tool_start", "name": "run_shell", "arguments": {"command": "echo hi"}}
            )
            await pilot.pause()
            tool_view = app._tool_view
            check("工具块已渲染", isinstance(tool_view, ToolCallView))
            check("工具块默认折叠", tool_view is not None and tool_view.collapsed is True)
            check("工具概览含名称与命令",
                  tool_view is not None and "run_shell" in tool_view.title and "echo hi" in tool_view.title)
            tool_body = tool_view.query_one(".tool-body")
            check("工具参数进折叠体", "echo hi" in str(tool_body.content))
            check("运行中概览带省略号", tool_view is not None and "…" in tool_view.title)

            await app._handle_event(
                {"type": "tool_result", "name": "run_shell", "output": "hi", "ok": True}
            )
            await pilot.pause()
            check("工具结果显示完成态", "✓" in tool_view.title)
            check("工具结果进折叠体", "hi" in str(tool_body.content))
            check("完成后视图指针复位", app._tool_view is None)

            # A result without a matching start (refused confirmation,
            # sub-agent summary) becomes a self-contained finished block.
            before_tools = len(chat.children)
            await app._handle_event(
                {"type": "tool_result", "name": "write_file",
                 "output": "[已拒绝] 用户拒绝执行该操作", "ok": False}
            )
            await pilot.pause()
            standalone = next(
                (c for c in reversed(chat.children) if isinstance(c, ToolCallView)), None
            )
            check("无 start 的结果独立成块",
                  standalone is not None and standalone is not tool_view
                  and standalone is not None and "✗" in standalone.title,
                  str(standalone.title if standalone else None))
            standalone_body = standalone.query_one(".tool-body")
            check("独立块折叠体含结果", "已拒绝" in str(standalone_body.content))
            check("独立块不占运行指针", app._tool_view is None)

            # Consecutive collapsed tool blocks must not have a blank line
            # between them: Collapsible's default padding-bottom + our
            # margin-top would add up to two empty rows.
            await app._handle_event(
                {"type": "tool_start", "name": "web_search", "arguments": {"query": "x"}}
            )
            await pilot.pause()
            tight_view = app._tool_view
            check("连续工具块紧凑无空行",
                  tight_view is not None and "-collapsed" in tight_view.classes
                  and tuple(tight_view.styles.margin)[0] == 0
                  and tuple(tight_view.styles.padding)[2] == 0,
                  f"margin={tight_view.styles.margin if tight_view else None} "
                  f"padding={tight_view.styles.padding if tight_view else None}")

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
