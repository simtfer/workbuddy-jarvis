"""The Textual application: JARVIS' face."""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Input, Static

from .. import __version__
from ..config import Config, load_config
from ..core.agent import Agent, ConfirmRequest
from ..core.scheduler import ScheduledTask, Scheduler, ScheduleError, TaskStore, parse_task_command
from ..memory.store import HistoryStore, default_db_path
from ..providers import SEARCH_PROVIDERS
from ..textwidth import clip, pad
from ..tools import build_registry, sysinfo
from ..tools import clipboard as clipboard_mod
from ..tools import notify as notify_mod
from ..tools import procman, web
from .screens import ConfirmScreen, HelpScreen, ModelPickerScreen
from .widgets import (
    AssistantMessage,
    Banner,
    CommandMenu,
    MenuCommand,
    MenuDismiss,
    MENU_RAIL_WIDTH,
    MENU_WIDTH,
    Notice,
    PlanView,
    ThinkingView,
    ToolCallView,
    UserMessage,
    menu_head,
)


def split_flags(text: str) -> tuple[list[str], dict[str, str]]:
    """``"gpt --provider openai --model gpt-4o"`` -> (["gpt"], {...}).

    ``--flag value`` pairs go into the dict; ``--bare`` becomes ``{"bare": ""}``.
    """

    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    positional: list[str] = []
    flags: dict[str, str] = {}
    pending: str | None = None
    for token in tokens:
        if token.startswith("--"):
            pending = token[2:].lower()
            flags.setdefault(pending, "")
        elif pending is not None:
            flags[pending] = token
            pending = None
        else:
            positional.append(token)
    return positional, flags


# figlet "ansi_shadow" of JARVIS lives in widgets.py with the Banner widget that
# falls back to a compact wordmark when the chat pane is too narrow for it.


class JarvisApp(App[None]):
    """Windows resident assistant."""

    TITLE = "JARVIS-Win"
    SUB_TITLE = f"v{__version__} · 说人话，也干活"

    # Focus the prompt box on boot. The chat pane is a scrollable container and
    # therefore focusable, so leaving this to the default auto-focus can park the
    # caret somewhere the user cannot type.
    AUTO_FOCUS = "#prompt"

    # The sidebar geometry lives in widgets.py (the rows are padded to those
    # numbers), so the CSS widths are spliced in rather than typed twice.
    CSS = """
    #body { height: 1fr; }
    #menu {
        width: __MENU_WIDTH__; padding: 0 1; border-right: solid $panel;
        color: $text-muted; background: transparent;
    }
    #menu.collapsed { width: __MENU_RAIL_WIDTH__; padding: 0 0; }
    #menu-rail { display: none; color: $accent; padding: 1 0 0 1; }
    #menu.collapsed #menu-rail { display: block; }
    #menu-box { height: 1fr; }
    #menu.collapsed #menu-box { display: none; }
    #menu-head { color: $text-muted; padding: 0 0 1 0; }
    #menu-list {
        height: 1fr; border: none; padding: 0; background: transparent;
    }
    #menu-list:focus { border: none; }
    #chat { width: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
    #side {
        width: 46; padding: 0 1; border-left: solid $panel; color: $text-muted;
    }
    #side.hidden { display: none; }
    #banner { color: $accent; padding: 1 0 0 0; }
    .user-message {
        color: $text; background: $primary 25%; border-left: thick $primary;
        padding: 0 1; margin: 1 0 0 0;
    }
    .assistant-message { margin: 1 0 0 0; padding: 0 1; background: transparent; }
    .thinking {
        margin: 1 0 0 0; padding: 0 1; border-left: solid $panel;
        background: $panel 20%;
    }
    /* Collapsed blocks sit flush under whatever is above (tool rows, another
       thinking block); only an expanded block keeps the breathing room. */
    .thinking.-collapsed { margin-top: 0; padding-bottom: 0; }
    .thinking-body { color: $text-muted; padding: 0 0 1 0; }
    .tool-call { margin-top: 1; background: transparent; border-top: none; }
    /* Collapsed tool blocks sit directly under each other - Collapsible's
       default padding-bottom would leave a blank line between two of them.
       An expanded block keeps the breathing room via the base rule above. */
    .tool-call.-collapsed { margin-top: 0; padding-bottom: 0; }
    .tool-call CollapsibleTitle { color: $warning; background: transparent; padding: 0 1; }
    .tool-call.bad CollapsibleTitle { color: $error; }
    .tool-body { color: $text-muted; padding: 0 0 1 0; }
    .notice { color: $text-muted; padding: 0 1; margin-top: 1; }
    .notice.bad { color: $error; }
    .notice.warn { color: $warning; }
    .plan-view {
        color: $accent; border: round $accent 40%; padding: 0 1; margin-top: 1;
    }
    #prompt { dock: bottom; }
    """.replace("__MENU_WIDTH__", str(MENU_WIDTH)).replace(
        "__MENU_RAIL_WIDTH__", str(MENU_RAIL_WIDTH)
    )

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出"),
        Binding("ctrl+l", "clear_chat", "清屏"),
        Binding("ctrl+b", "toggle_menu", "命令菜单"),
        Binding("ctrl+s", "toggle_side", "系统面板"),
        Binding("ctrl+x", "cancel", "取消任务"),
        Binding("f1", "help", "帮助"),
        Binding("f2", "tasks", "定时任务"),
    ]

    def __init__(
        self,
        config: Config,
        *,
        scheduler_enabled: bool = True,
        db_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.store = HistoryStore(db_path or default_db_path())
        self.session_id = self.store.new_session_id()
        self.store.ensure_session(self.session_id, config.default_model)
        self.task_store = TaskStore(self.store.conn)
        self.registry = build_registry(config.security, str(config.workdir), config.search)
        self.agent = Agent(
            config=config,
            registry=self.registry,
            model_name=config.default_model,
            confirm_handler=self._confirm,
            session_id=self.session_id,
            on_message=self._persist,
            facts_provider=self._facts_texts,
        )
        self.scheduler = (
            Scheduler(self.task_store, runner=self._run_scheduled_task)
            if scheduler_enabled and config.daemon.scheduler
            else None
        )

        self._session_allow: set[str] = set()
        self._stream_widget: AssistantMessage | None = None
        self._thinking_widget: ThinkingView | None = None
        self._tool_view: ToolCallView | None = None
        self._plan_view: PlanView | None = None
        self._busy = False
        self._panel_busy = False
        self._top_busy = False
        self._panel_data: dict[str, str] | None = None
        self._top_text = "(正在采集…)"
        self._last_flush = 0.0
        self._tool_count = 0
        self._turns_since_learn = 0
        self._notify_enabled = config.daemon.notify

    # ---------------------------------------------------------------- lifecycle
    def compose(self) -> ComposeResult:
        """Left menu, chat, right system panel - the two sidebars start collapsed.

        The menu replaced the key-hint footer, so the hints live in its head
        block; the panels start out of the way because the chat is the point.
        """

        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="menu", classes="collapsed"):
                yield Static("≡", id="menu-rail", markup=False)
                with Vertical(id="menu-box"):
                    yield Static(menu_head(), id="menu-head", markup=False)
                    yield CommandMenu(id="menu-list")
            with VerticalScroll(id="chat"):
                yield Banner(id="banner")
            with Vertical(id="side", classes="hidden"):
                yield Static("系统面板已收起 · Ctrl+S 打开", id="syspanel", markup=False)
        yield Input(placeholder="问我任何事，或输入 /help（Ctrl+B 打开命令菜单）", id="prompt")

    def on_mount(self) -> None:
        self.theme = "textual-dark"
        self._focus_prompt()
        self.set_interval(2.0, self._refresh_panel)
        # Walking every process is much more expensive than the metrics above,
        # so the TOP 进程 table refreshes on its own, slower clock.
        self.set_interval(10.0, self._refresh_processes)
        if not self._side_hidden():
            self._refresh_panel()
            self._refresh_processes()
        if self.scheduler is not None:
            self.scheduler.on_error = self._on_schedule_error
            self.scheduler.start()
        self.run_worker(self._greet(), name="greet")

    def _focus_prompt(self) -> None:
        """Put the caret back in the prompt box - modals take the focus with them."""

        try:
            self.query_one("#prompt", Input).focus()
        except Exception:  # noqa: BLE001 - the widget is gone while shutting down
            pass

    def _on_schedule_error(self, task: ScheduledTask, error: str) -> None:
        self.run_worker(self._append(Notice(f"任务 #{task.id} 出错：{error}", "bad")))

    async def on_unmount(self) -> None:
        if self.scheduler is not None:
            await self.scheduler.stop()
        await self.agent.aclose()
        self.store.close()

    def _persist(self, message: dict[str, Any]) -> None:
        role = message.get("role")
        content = message.get("content")
        if role in {"user", "assistant"} and content:
            self.store.add_message(self.session_id, str(role), str(content))
            if role == "user":
                self.store.set_title(self.session_id, str(content).strip().replace("\n", " "))

    def _facts_texts(self) -> list[str]:
        return [text for _id, text in self.store.facts(self.config.memory.max_facts_in_prompt)]

    async def _greet(self) -> None:
        model = self.agent.model
        key_ok = (
            bool(model.resolve_api_key())
            or "localhost" in model.base_url
            or "127.0.0.1" in model.base_url
        )
        await self._append(
            Notice(
                f"JARVIS 已就绪 · 模型 {model.display}（{model.model}） · "
                f"工具 {len(self.registry.tools)} 个 · 记忆 {self.store.fact_count()} 条"
                "\nCtrl+B 打开左侧命令菜单 · Ctrl+S 打开系统面板（两个侧栏默认都收起）",
                "info",
            )
        )
        chain = self.config.fallbacks_for(self.agent.model_key)
        if len(chain) > 1:
            await self._append(
                Notice(f"主模型不可用时按顺序自动切：{' → '.join(chain)}（/model fallback 可改）", "info")
            )
        if self.config.search.enabled:
            await self._append(Notice(f"联网搜索 {web.describe(self.config.search)}", "info"))
        if self.config.created:
            await self._append(
                Notice(f"已生成配置文件 {self.config.path}，请填写 API Key 后重启或换模型。", "warn")
            )
        if not key_ok:
            await self._append(
                Notice(
                    f"⚠ 模型 {model.display} 还没有 API Key："
                    f"在 config.toml 填入 api_key，或设置环境变量 {model.api_key_env or 'API_KEY'}。"
                    " 期间 /sys /tools /facts 等本地命令仍可用。",
                    "bad",
                )
            )

        tasks = [task for task in self.task_store.list() if task.enabled]
        if self.scheduler is None:
            await self._append(
                Notice("调度器已关闭（--no-scheduler 或 config.toml 的 daemon.scheduler=false）。", "info")
            )
        elif tasks:
            upcoming = sorted(tasks, key=lambda t: t.next_run)[0]
            await self._append(
                Notice(
                    f"{len(tasks)} 个定时任务在跑，最近一个：#{upcoming.id} {upcoming.title}"
                    f"（{upcoming.schedule_text}，{upcoming.next_run or '待定'}）。F2 查看。",
                    "info",
                )
            )

        last = self.store.last_session()
        sessions, messages = self.store.stats()
        if last and last[0] != self.session_id:
            await self._append(
                Notice(
                    f"本地已存 {sessions} 个会话 / {messages} 条消息，"
                    f"最近一次：{last[0]}（{last[1] or '无标题'}）。/history 查看，/resume <id> 载入。",
                    "info",
                )
            )

    # ------------------------------------------------------------------- input
    @on(Input.Submitted, "#prompt")
    async def _on_submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            await self._run_command(text)
            return
        if self._busy:
            self.notify("上一个任务还在执行，按 Ctrl+X 可以取消。", severity="warning")
            return
        self.run_turn(text)

    # ------------------------------------------------------------------- turn
    @work(exclusive=True, group="chat")
    async def run_turn(self, text: str) -> None:
        await self._append(UserMessage(text))
        async for _event in self._stream_agent(lambda: self.agent.run(text)):
            pass
        await self._learn()

    @work(exclusive=True, group="chat")
    async def run_plan(self, task: str, execute: bool = True) -> None:
        await self._append(UserMessage(f"/plan {task}"))
        async for _event in self._stream_agent(lambda: self.agent.plan_task(task)):
            pass

        if execute and self.agent.plan:
            await self._append(Notice("计划已生成，开始逐步执行。", "info"))
            async for _event in self._stream_agent(self.agent.execute_plan):
                pass

    async def _stream_agent(self, factory):
        """Consume an agent async-generator, rendering events; returns the events."""

        self._busy = True
        self._tool_count = 0
        self._stream_widget = None
        self._thinking_widget = None
        self._tool_view = None
        started = time.monotonic()
        try:
            async for event in factory():
                await self._handle_event(event)
                yield event
        except asyncio.CancelledError:
            await self._append(Notice("已中断本次任务。", "warn"))
            raise
        except Exception as exc:  # noqa: BLE001
            await self._append(Notice(f"运行出错：{type(exc).__name__}: {exc}", "bad"))
        finally:
            self._busy = False
            self._stream_widget = None
            self._tool_view = None
            if self._thinking_widget is not None:
                self._thinking_widget.finish()
                self._thinking_widget = None
            elapsed = time.monotonic() - started
            if self._tool_count:
                await self._append(
                    Notice(f"· 本次调用 {self._tool_count} 个工具，用时 {elapsed:.1f}s", "info")
                )

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "thinking":
            # Chain-of-thought stream from a reasoning model. One collapsed
            # block per model round: a fresh one is created after every tool
            # call, mirroring how the model re-thinks between rounds.
            if self._thinking_widget is None:
                self._thinking_widget = ThinkingView()
                await self._append(self._thinking_widget)
            self._thinking_widget.append_text(event["delta"])
        elif kind == "text":
            if self._thinking_widget is not None:
                # The visible answer started, so this thinking round is over.
                self._thinking_widget.finish()
                self._thinking_widget = None
            if self._stream_widget is None:
                self._stream_widget = AssistantMessage()
                await self._append(self._stream_widget)
            now = time.monotonic()
            if now - self._last_flush >= 0.05:
                self._last_flush = now
                await self._stream_widget.append_text(event["text"])
            else:
                self._stream_widget.buffer += event["text"]
        elif kind == "tool_start":
            self._stream_widget = None
            self._close_thinking()
            self._tool_count += 1
            args = event.get("arguments") or {}
            detail = str(args.get("command") or args.get("path") or "")[:90]
            self._tool_view = ToolCallView(event["name"], detail, args)
            await self._append(self._tool_view)
        elif kind == "tool_result":
            self._stream_widget = None
            self._close_thinking()
            ok = bool(event.get("ok"))
            if self._tool_view is not None:
                # Fold the result into the block opened by the matching start.
                self._tool_view.set_result(event["output"], ok)
                self._tool_view = None
            else:
                # A result with no start (refused confirmation, sub-agent
                # summary): a self-contained block in finished form.
                await self._append(
                    ToolCallView(event["name"], "", output=event["output"], ok=ok)
                )
        elif kind == "notice":
            await self._append(Notice(event["text"], "info"))
        elif kind == "plan":
            self._plan_view = PlanView(event["task"], event["steps"])
            await self._append(self._plan_view)
            await self._append(
                Notice(
                    f"共 {len(event['steps'])} 步。执行：/do（逐步跑完）· 调整：/reset 后重新 /plan。",
                    "info",
                )
            )
        elif kind == "step_start":
            self._stream_widget = None
            if self._plan_view is not None:
                self._plan_view.set_status(event["index"], "running")
            await self._append(
                Notice(f"▶ 第 {event['index'] + 1}/{event['total']} 步：{event['title']}", "info")
            )
        elif kind == "step_done":
            if self._plan_view is not None:
                self._plan_view.set_status(event["index"], event["status"])
        elif kind == "plan_done":
            await self._append(
                Notice(f"计划执行结束：{event['done']}/{event['total']} 步成功。", "info")
            )
        elif kind == "subagent_start":
            prompts = event.get("prompts") or []
            preview = " · ".join(p[:30] for p in prompts[:5])
            if len(prompts) > 5:
                preview += f" … 等 {len(prompts)} 个"
            await self._append(
                Notice(f"→ 派发了 {event['count']} 个子任务：{preview}", "info")
            )
        elif kind == "subagent_done":
            status = event.get("status", "done")
            mark = {"done": "✓", "error": "✗", "timeout": "⏱"}.get(status, "·")
            tools = event.get("tool_count", 0)
            elapsed = event.get("elapsed", 0.0)
            await self._append(
                Notice(
                    f"{mark} 子任务 #{event['index'] + 1} {status} · "
                    f"{tools} 工具 · {elapsed:.1f}s",
                    "info" if status == "done" else ("warn" if status == "timeout" else "bad"),
                )
            )
        elif kind == "subagent_timeout":
            results = event.get("results") or []
            finished = sum(1 for r in results if r["status"] == "done")
            running = [r["index"] + 1 for r in results if r["status"] not in ("done", "error")]
            timeout = event.get("default_timeout", 0)
            await self._append(
                Notice(
                    f"⏱ 默认超时 {timeout:.0f}s：已完成 {finished}/{len(results)}，"
                    f"仍在跑：{', '.join('#' + str(i) for i in running) or '无'}。"
                    f"  先把已有结果继续推进，剩下的完成后会自动追加最终汇总。",
                    "warn",
                )
            )
        elif kind == "subagent_final":
            results = event.get("results") or []
            finished = sum(1 for r in results if r["status"] == "done")
            failed = sum(1 for r in results if r["status"] == "error")
            await self._append(
                Notice(
                    f"🏁 子任务全部结束：{finished} 成功"
                    + (f" · {failed} 失败" if failed else "")
                    + "。",
                    "info" if not failed else "warn",
                )
            )
        elif kind == "subagent_summary":
            # Rendered as part of the tool_result that follows.
            pass
        elif kind == "error":
            self._stream_widget = None
            self._close_thinking()
            await self._append(Notice(event["message"], "bad"))
        elif kind == "done":
            self._close_thinking()
            self._tool_view = None
            if self._stream_widget is not None:
                await self._stream_widget.append_text("")

    def _close_thinking(self) -> None:
        """Seal the current thinking block; the next round gets a fresh one."""

        if self._thinking_widget is not None:
            self._thinking_widget.finish()
            self._thinking_widget = None

    # -------------------------------------------------------------- permission
    async def _confirm(self, request: ConfirmRequest) -> bool:
        if request.tool in self._session_allow:
            await self._append(Notice(f"会话白名单放行：{request.tool}", "info"))
            return True
        if not self.is_running:
            return False

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()

        def _resolved(choice: str | None) -> None:
            if future.done():
                return
            if choice == "always":
                self._session_allow.add(request.tool)
                future.set_result(True)
            else:
                future.set_result(choice == "approve")

        try:
            self.push_screen(ConfirmScreen(request), callback=_resolved)
        except Exception as exc:  # noqa: BLE001 - never leave the agent hanging
            await self._append(Notice(f"确认框打开失败（{exc}），已按拒绝处理。", "bad"))
            return False

        try:
            # Safety valve: an unattended prompt must not block the agent forever.
            return await asyncio.wait_for(future, timeout=300)
        except TimeoutError:
            await self._append(Notice("确认超时（5 分钟无响应），已按拒绝处理。", "warn"))
            return False

    # --------------------------------------------------------------- commands
    async def _run_command(self, raw: str) -> None:
        parts = raw.split(maxsplit=1)
        command = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        args = rest.split()

        if command in {"/quit", "/exit", "/q"}:
            self.exit()
        elif command in {"/help", "/?"}:
            self.push_screen(HelpScreen(), callback=lambda _result: self._focus_prompt())
        elif command == "/clear":
            await self.action_clear_chat()
        elif command == "/sys":
            self.run_sys_report()
        elif command == "/clip":
            await self._handle_clip_command(rest)
        elif command == "/ps":
            await self._handle_ps_command(rest)
        elif command == "/tools":
            lines = [
                f"· {tool.name:<14} {'⚠ 需确认' if tool.dangerous else '免确认'}  {tool.description[:60]}"
                for tool in self.registry.tools
            ]
            await self._append(Notice("可用工具：\n" + "\n".join(lines), "info"))
        elif command in {"/model", "/models"}:
            await self._handle_model_command(rest)
        elif command in {"/provider", "/providers"}:
            await self._handle_provider_command(rest)
        elif command == "/search":
            await self._handle_search_command(rest)
        elif command == "/fetch" and rest:
            await self._fetch_page(rest)
        elif command == "/history":
            await self._show_history()
        elif command == "/resume" and rest:
            await self._resume(args[0])
        elif command == "/reset":
            self.agent.reset()
            await self._append(Notice("上下文已清空（历史与记忆仍保存在 data/ 中）。", "info"))
        elif command == "/theme":
            self.theme = "textual-light" if self.theme == "textual-dark" else "textual-dark"
            await self._append(Notice(f"主题：{self.theme}", "info"))
        # ---------------------------------------------------------- memory
        elif command == "/facts":
            await self._show_facts()
        elif command == "/remember" and rest:
            await self._remember(rest)
        elif command == "/forget" and rest:
            await self._forget(args[0])
        elif command == "/learn":
            await self._learn(force=True)
        # ------------------------------------------------------------ plan
        elif command == "/plan" and rest:
            self.run_plan(rest, execute=False)
        elif command == "/auto" and rest:
            self.run_plan(rest, execute=True)
        elif command == "/do":
            if not self.agent.plan:
                await self._append(Notice("当前没有计划，先用 /plan <任务> 生成。", "warn"))
            else:
                self.run_plan_execute()
        # ------------------------------------------------------------ tasks
        elif command == "/task" or command == "/tasks":
            await self._handle_task_command(rest)
        elif command == "/daemon":
            hotkey = self.config.daemon.hotkey
            tray = "开" if self.config.daemon.tray else "关"
            await self._append(
                Notice(
                    "常驻守护：`uv run jarvis --daemon`\n"
                    f"  · 全局热键 {hotkey} 唤起新窗口\n"
                    f"  · 系统托盘图标：{tray}（右键菜单：打开 JARVIS / 定时任务 / 状态 / 退出）\n"
                    "  · 后台跑定时任务（当前窗口若也要接管，直接启动 TUI 即可）",
                    "info",
                )
            )
        else:
            await self._append(Notice(f"未知命令 {command}，/help 查看全部命令。", "warn"))

    # ------------------------------------------------------------------- models
    async def _handle_model_command(self, rest: str) -> None:
        positional, flags = split_flags(rest)
        # A bare ``/model`` means "let me pick", not "print a table" - so the
        # default here must be empty, not "list".
        action = positional[0].lower() if positional else ""
        payload = positional[1:] if len(positional) > 1 else []

        if action == "":
            await self._pick_model()
        elif action == "list":
            await self._show_models()
        elif action == "add":
            await self._add_model(payload, flags)
        elif action in {"rm", "del", "remove"}:
            await self._remove_model(payload, flags)
        elif action in {"default", "use"}:
            await self._set_default_model(payload)
        elif action == "fallback":
            await self._set_fallbacks(payload)
        else:
            await self._switch_model(" ".join(positional))

    async def _show_models(self) -> None:
        current = self.agent.model_key
        names = self.config.model_names()
        lines = []
        for index, name in enumerate(names, start=1):
            model = self.config.models[name]
            mark = "▶" if name == current else " "
            key_state = "已配 Key" if model.key_ready else "⚠ 缺 Key"
            provider = f"[{model.provider}]" if model.provider else "[自定义]"
            lines.append(f"{mark} {index}. {name:<10} {provider:<13} {model.model:<26} {key_state}")
        chain = " → ".join(self.config.fallbacks_for(current)) or "（自动挑已配 Key 的）"
        await self._append(
            Notice(
                "模型列表（/model 直接选，或 /model <序号|名字> 热切换）：\n"
                + "\n".join(lines)
                + f"\n\n当前 ▶ {current} · 故障自动切换：{chain}"
                + "\n新增：/model add <名字> --provider <provider> --model <模型ID> [--env VAR]"
                + " [--key sk-xxx] [--url ...] [--timeout 秒] [--default]",
                "info",
            )
        )

    async def _pick_model(self) -> None:
        """Arrow-key model picker, so switching does not require typing a name."""

        if self._busy:
            await self._append(Notice("正在跑任务，等它结束再切模型（Ctrl+X 可取消）。", "warn"))
            return
        current = self.agent.model_key
        choices: list[tuple[str, str]] = []
        for name in self.config.model_names():
            model = self.config.models[name]
            mark = "▶" if name == current else " "
            key_state = "已配 Key" if model.key_ready else "缺 Key"
            choices.append(
                (
                    name,
                    f"{mark} {name}  ·  {model.provider or '自定义'}  ·  {model.model}  ·  {key_state}",
                )
            )

        def _resolved(picked: str | None) -> None:
            # Do NOT await anything here: this callback fires from the prompt's
            # message handler, and blocking that freezes the whole UI. Hand the
            # switch to a worker instead.
            self._focus_prompt()
            if picked:
                self.run_worker(
                    self._switch_model(picked), name="model-pick", group="model"
                )

        try:
            self.push_screen(ModelPickerScreen(choices, current), callback=_resolved)
        except Exception as exc:  # noqa: BLE001 - never wedge the prompt on a broken modal
            await self._append(Notice(f"模型选择器打开失败（{exc}）。", "bad"))

    async def _switch_model(self, token: str) -> None:
        if self._busy:
            await self._append(Notice("正在跑任务，等它结束再切模型（Ctrl+X 可取消）。", "warn"))
            return
        previous = self.agent.model_key
        try:
            key = self.agent.set_model(token)
        except KeyError as exc:
            await self._append(Notice(str(exc.args[0]), "warn"))
            return
        model = self.config.models[key]
        if key == previous:
            await self._append(Notice(f"已经是 {key}（{model.model}）了。", "info"))
            return
        # Switching in the TUI means "this is the model I want to use", so it is
        # persisted - reopening JARVIS must not silently fall back to the old one.
        self.config.set_default(key)
        await self._append(
            Notice(
                f"已切换到 {key} · {model.model}"
                f"（{model.base_url}）{' ⚠ 还没配 Key' if not model.key_ready else ''}"
                f"\n已设为默认模型（下次启动仍是它），上下文 {len(self.agent.history())} 条消息保持不变。",
                "info" if model.key_ready else "warn",
            )
        )

    async def _add_model(self, payload: list[str], flags: dict[str, str]) -> None:
        name = (payload[0] if payload else flags.get("name", "")).strip()
        if not name:
            await self._append(
                Notice(
                    "用法：/model add <名字> --provider <provider> --model <模型ID>"
                    " [--env VAR] [--key sk-xxx] [--url https://...] [--label 显示名] [--timeout 秒] [--default]\n"
                    "例：/model add gpt --provider openai --model gpt-4o-mini --env OPENAI_API_KEY",
                    "warn",
                )
            )
            return
        try:
            cfg = self.config.add_model(
                name,
                provider=flags.get("provider", ""),
                model=flags.get("model", ""),
                label=flags.get("label", ""),
                base_url=flags.get("url", "") or flags.get("base-url", ""),
                api_key=flags.get("key", ""),
                api_key_env=flags.get("env", ""),
                temperature=float(flags.get("temperature", 0.3) or 0.3),
                timeout=float(flags.get("timeout", 180.0) or 180.0),
                make_default="default" in flags,
            )
        except (ValueError, KeyError) as exc:
            await self._append(Notice(str(exc.args[0] if exc.args else exc), "warn"))
            return
        self.agent.config = self.config
        await self._append(
            Notice(
                f"已写入模型 {cfg.name}：{cfg.model} @ {cfg.base_url}\n"
                f"配置已保存到 {self.config.path}。切换：/model {cfg.name}"
                + ("（已设为默认）" if "default" in flags else ""),
                "info",
            )
        )

    async def _remove_model(self, payload: list[str], flags: dict[str, str]) -> None:
        token = (payload[0] if payload else flags.get("name", "")).strip()
        if not token:
            await self._append(Notice("用法：/model rm <名字>", "warn"))
            return
        key = self.config.find_model(token) or token
        if key == self.agent.model_key:
            await self._append(Notice("正在用的是这个模型，先 /model 切到别的再删。", "warn"))
            return
        try:
            removed = self.config.remove_model(key)
        except ValueError as exc:
            await self._append(Notice(str(exc), "warn"))
            return
        await self._append(
            Notice(f"已删除模型 {key}。" if removed else f"没有模型 {key}。",
                   "info" if removed else "warn")
        )

    async def _set_default_model(self, payload: list[str]) -> None:
        token = payload[0] if payload else ""
        if not token:
            await self._append(Notice("用法：/model default <名字>（下次启动用它）", "warn"))
            return
        try:
            cfg = self.config.set_default(self.config.find_model(token) or token)
        except KeyError as exc:
            await self._append(Notice(str(exc.args[0]), "warn"))
            return
        await self._append(Notice(f"默认模型已设为 {cfg.name}（写回 config.toml）。", "info"))

    async def _set_fallbacks(self, payload: list[str]) -> None:
        if not payload:
            chain = ", ".join(self.config.fallback_models) or "（未设置，运行时自动挑选）"
            await self._append(
                Notice(f"故障切换链：{chain}\n用法：/model fallback qwen ollama（写回 config.toml）", "info")
            )
            return
        resolved: list[str] = []
        for token in payload:
            key = self.config.find_model(token)
            if key:
                resolved.append(key)
        self.config.fallback_models = resolved
        self.config.save()
        await self._append(
            Notice(f"故障切换链已设为：{' → '.join(resolved) or '（空，自动挑选）'}", "info")
        )

    # ---------------------------------------------------------------- providers
    async def _handle_provider_command(self, rest: str) -> None:
        positional, flags = split_flags(rest)
        action = positional[0].lower() if positional else "list"
        payload = positional[1:] if len(positional) > 1 else []

        if action in {"list", ""}:
            await self._show_providers()
        elif action == "add":
            await self._add_provider(payload, flags)
        elif action in {"rm", "del", "remove"}:
            await self._remove_provider(payload)
        else:
            await self._show_provider(action)

    async def _show_providers(self) -> None:
        in_use: dict[str, list[str]] = {}
        for name, model in self.config.models.items():
            if model.provider:
                in_use.setdefault(model.provider, []).append(name)

        lines = ["对话模型（/model add <名字> --provider <名字> --model <模型ID>）："]
        for name in self.config.catalog.names():
            preset = self.config.catalog.require(name)
            custom = "※自定义" if name in self.config.user_providers else ""
            used = f" · 在用：{','.join(in_use[name])}" if name in in_use else ""
            key_state = ""
            if preset.api_key_env:
                ready = bool(os.environ.get(preset.api_key_env, "").strip())
                key_state = f" · {preset.api_key_env}{'✓' if ready else '（未设置）'}"
            lines.append(f"  {name:<12} {preset.label:<22} {preset.base_url}{used}{key_state}{custom}")

        lines.append("")
        lines.append("搜索后端（/search backend <名字>）：")
        for name, preset in sorted(SEARCH_PROVIDERS.items()):
            mark = "▶" if name == self.config.search.provider else " "
            key_state = ""
            if preset.needs_key:
                key_state = " " + ("Key ✓" if self.config.search.resolve_api_key() else "⚠ 缺 Key")
            lines.append(f"{mark} {name:<12} {preset.label}{key_state}")

        lines.append("")
        lines.append(
            "自定义 endpoint：/provider add <名字> <base_url> [--env VAR] [--label 显示名]"
            "\n 例：/provider add myvllm http://10.0.0.5:8000/v1 --env MYVLLM_KEY --label 公司内网"
        )
        await self._append(Notice("\n".join(lines), "info"))

    async def _show_provider(self, name: str) -> None:
        preset = self.config.catalog.get(name)
        if preset is None:
            await self._append(Notice(f"没有 provider '{name}'，/provider list 看全部。", "warn"))
            return
        using = [n for n, m in self.config.models.items() if m.provider == name]
        await self._append(
            Notice(
                f"{name} · {preset.label}\n"
                f"base_url     {preset.base_url}\n"
                f"api_key_env  {preset.api_key_env or '(不需要)'}\n"
                f"常见模型     {', '.join(preset.models) or '(未列)'}\n"
                f"控制台       {preset.console or '-'}\n"
                f"在用模型     {', '.join(using) or '无'}"
                + (f"\n备注         {preset.note}" if preset.note else ""),
                "info",
            )
        )

    async def _add_provider(self, payload: list[str], flags: dict[str, str]) -> None:
        name = (payload[0] if payload else flags.get("name", "")).strip()
        base_url = (payload[1] if len(payload) > 1 else flags.get("url", "")).strip()
        if not name or not base_url:
            await self._append(
                Notice(
                    "用法：/provider add <名字> <base_url> [--env VAR] [--label 显示名]\n"
                    "例：/provider add myvllm http://10.0.0.5:8000/v1 --env MYVLLM_KEY",
                    "warn",
                )
            )
            return
        try:
            preset = self.config.add_provider(
                name,
                base_url=base_url,
                label=flags.get("label", ""),
                api_key_env=flags.get("env", ""),
            )
        except ValueError as exc:
            await self._append(Notice(str(exc), "warn"))
            return
        await self._append(
            Notice(
                f"已添加 provider {preset.name} · {preset.label} → {preset.base_url}\n"
                f"接着加模型：/model add <名字> --provider {preset.name} --model <模型ID>",
                "info",
            )
        )

    async def _remove_provider(self, payload: list[str]) -> None:
        name = payload[0] if payload else ""
        if not name:
            await self._append(Notice("用法：/provider rm <名字>（只删自定义的）", "warn"))
            return
        removed = self.config.remove_provider(name)
        await self._append(
            Notice(f"已删除自定义 provider {name}。" if removed else f"{name} 不是自定义 provider，删不掉。",
                   "info" if removed else "warn")
        )

    # -------------------------------------------------------------------- web
    async def _handle_search_command(self, rest: str) -> None:
        positional, flags = split_flags(rest)
        action = positional[0].lower() if positional else ""

        if action in {"backend", "provider"} and len(positional) > 1:
            name = positional[1].lower()
            if name not in SEARCH_PROVIDERS:
                await self._append(
                    Notice(f"未知搜索后端 '{name}'。可用：{', '.join(sorted(SEARCH_PROVIDERS))}", "warn")
                )
                return
            preset = SEARCH_PROVIDERS[name]
            self.config.search.provider = name
            self.config.search.base_url = ""  # fall back to the preset endpoint
            self.config.search.api_key_env = preset.api_key_env
            self.config.save()
            self.registry = build_registry(self.config.security, str(self.config.workdir), self.config.search)
            await self._append(
                Notice(f"搜索后端已切到 {name} · {preset.label}（已写回 config.toml）", "info")
            )
            return

        if not rest.strip():
            await self._append(
                Notice(
                    "联网搜索\n"
                    f"  当前后端  {web.describe(self.config.search)}\n"
                    "  用法      /search 关键词          直接搜一次\n"
                    "            /search backend tavily 换后端（bocha 中文好、tavily 摘要干净）\n"
                    "            /fetch example.com     打开网页读正文\n"
                    "  会话里直接说「搜一下 xxx」，JARVIS 会自己调 web_search 工具。",
                    "info",
                )
            )
            return

        query = " ".join(positional)
        await self._append(Notice(f"🔍 搜索「{query}」（{self.config.search.provider}）…", "info"))
        output = await self.registry.call(
            "web_search", {"query": query, "max_results": self.config.search.max_results}
        )
        await self._append(Notice(output, "info"))

    async def _fetch_page(self, url: str) -> None:
        await self._append(Notice(f"🌐 抓取 {url} …", "info"))
        output = await self.registry.call("fetch_url", {"url": url, "max_chars": 6000})
        await self._append(Notice(output, "info"))

    async def _show_history(self) -> None:
        rows = self.store.sessions(15)
        if not rows:
            await self._append(Notice("还没有历史会话。", "info"))
            return
        lines = [f"{sid}  {title or '(无标题)'}" for sid, title, _ts in rows]
        await self._append(Notice("最近会话（/resume <id> 载入上下文）：\n" + "\n".join(lines), "info"))

    async def _resume(self, target: str) -> None:
        history = self.store.recent_messages(target, self.config.security.history_limit)
        if not history:
            await self._append(Notice(f"会话 {target} 没有可载入的消息。", "warn"))
            return
        self.agent.load_history(history)
        await self._append(Notice(f"已载入会话 {target} 的 {len(history)} 条消息作为上下文。", "info"))

    # ------------------------------------------------------------------ memory
    async def _show_facts(self) -> None:
        rows = self.store.facts(50)
        if not rows:
            await self._append(
                Notice("长期记忆还是空的。用 /remember <内容> 手动添加，或正常聊天让它自己沉淀。", "info")
            )
            return
        lines = [f"#{fid:<3} {text}" for fid, text in rows]
        await self._append(
            Notice(f"长期记忆 {len(rows)} 条（/forget <编号> 删除）：\n" + "\n".join(lines), "info")
        )

    async def _remember(self, text: str) -> None:
        fact_id, created = self.store.add_fact(text, source=f"manual@{self.session_id}")
        self.agent.refresh_system()
        verb = "已记住" if created else "已存在，已刷新"
        await self._append(Notice(f"{verb} #{fact_id}：{text}", "info"))

    async def _forget(self, raw_id: str) -> None:
        try:
            fact_id = int(raw_id.lstrip("#"))
        except ValueError:
            await self._append(Notice(f"'{raw_id}' 不是有效的编号。", "warn"))
            return
        removed = self.store.delete_fact(fact_id)
        self.agent.refresh_system()
        await self._append(
            Notice(f"已删除记忆 #{fact_id}。" if removed else f"没有编号为 {fact_id} 的记忆。",
                   "info" if removed else "warn")
        )

    async def _learn(self, force: bool = False) -> None:
        if not force and not self.config.memory.auto_learn:
            return
        self._turns_since_learn += 1
        if not force and self._turns_since_learn < self.config.memory.learn_every:
            return
        self._turns_since_learn = 0
        transcript = self.agent.transcript()
        if not transcript.strip():
            return
        facts = await self.agent.learn(transcript)
        added: list[str] = []
        for fact in facts:
            _fid, created = self.store.add_fact(fact, source=f"auto@{self.session_id}")
            if created:
                added.append(fact)
        if added:
            self.agent.refresh_system()
            await self._append(
                Notice(f"沉淀了 {len(added)} 条长期记忆：\n" + "\n".join(f"+ {f}" for f in added), "info")
            )

    # ------------------------------------------------- clipboard / processes
    async def _handle_clip_command(self, rest: str) -> None:
        """``/clip`` 看剪贴板，``/clip set <文本>`` 写入，``/clip clear`` 清空。"""

        parts = rest.split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        payload = parts[1] if len(parts) > 1 else ""

        if action in {"", "show", "get"}:
            try:
                text = await asyncio.to_thread(clipboard_mod.read_clipboard, 4000)
            except Exception as exc:  # noqa: BLE001 - surface the reason in the UI
                await self._append(Notice(f"读剪贴板失败：{exc}", "bad"))
                return
            await self._append(Notice("剪贴板\n" + text, "info"))
        elif action in {"set", "put", "copy"}:
            if not payload:
                await self._append(Notice("用法：/clip set <要复制的文本>", "warn"))
                return
            try:
                result = await asyncio.to_thread(clipboard_mod.write_clipboard, payload)
            except Exception as exc:  # noqa: BLE001
                await self._append(Notice(f"写剪贴板失败：{exc}", "bad"))
                return
            await self._append(Notice(result, "info"))
        elif action in {"clear", "empty", "wipe"}:
            try:
                result = await asyncio.to_thread(clipboard_mod.clear_clipboard)
            except Exception as exc:  # noqa: BLE001
                await self._append(Notice(f"清空剪贴板失败：{exc}", "bad"))
                return
            await self._append(Notice(result, "info"))
        else:
            await self._append(
                Notice("用法：/clip（查看）· /clip set <文本>（写入）· /clip clear（清空）", "warn")
            )

    async def _handle_ps_command(self, rest: str) -> None:
        """``/ps [cpu|mem|name|pid] [关键词]`` —— 只读的进程列表。"""

        tokens = rest.split()
        sort_by = "cpu"
        if tokens and tokens[0].lower() in {"cpu", "mem", "memory", "name", "pid"}:
            sort_by = tokens.pop(0).lower()
        needle = " ".join(tokens)
        try:
            report = await asyncio.to_thread(procman.list_processes, sort_by, 15, needle)
        except Exception as exc:  # noqa: BLE001
            await self._append(Notice(f"列进程失败：{exc}", "bad"))
            return
        await self._append(Notice(report, "info"))

    # ------------------------------------------------------------------- tasks
    async def _handle_task_command(self, rest: str) -> None:
        parts = rest.split(maxsplit=1)
        action = parts[0].lower() if parts else "list"
        payload = parts[1].strip() if len(parts) > 1 else ""

        if action in {"list", ""}:
            await self._show_tasks()
        elif action == "add":
            await self._add_task(payload)
        elif action in {"rm", "del", "delete"}:
            await self._remove_task(payload)
        elif action in {"on", "off"}:
            await self._toggle_task(payload, action == "on")
        elif action == "run":
            await self._run_task_now(payload)
        else:
            await self._append(
                Notice(
                    "用法：/task list · /task add <内容> @daily 09:00 · /task rm <id>"
                    " · /task on|off <id> · /task run <id>",
                    "warn",
                )
            )

    async def _show_tasks(self) -> None:
        tasks = self.task_store.list()
        if not tasks:
            await self._append(
                Notice(
                    "还没有定时任务。示例：\n"
                    "/task add 看一下磁盘剩余空间 @daily 09:00\n"
                    "/task add 汇总今天新增的文件 @every 2h --danger（--danger 才允许执行写操作）",
                    "info",
                )
            )
            return
        lines = []
        for task in tasks:
            state = "启用" if task.enabled else "停用"
            next_run = task.next_run or "-"
            lines.append(
                f"#{task.id:<3} [{state}] {task.schedule_text:<12} 下次 {next_run:<17} "
                f"{'⚠' if task.allow_dangerous else ' '} {task.title}"
            )
        await self._append(Notice("定时任务：\n" + "\n".join(lines), "info"))

    async def _add_task(self, payload: str) -> None:
        if not payload:
            await self._append(Notice("用法：/task add <任务内容> @every 30m | @daily 09:00 | @once 2026-09-12 08:00", "warn"))
            return
        try:
            task = parse_task_command(payload)
        except ScheduleError as exc:
            await self._append(Notice(str(exc), "warn"))
            return
        self.task_store.add(task)
        await self._append(
            Notice(
                f"已添加任务 #{task.id}：{task.schedule_text} · {task.title}"
                f"（下次 {task.next_run}）{' · 允许危险操作' if task.allow_dangerous else ''}",
                "info",
            )
        )

    async def _remove_task(self, payload: str) -> None:
        task_id = self._parse_id(payload)
        if task_id is None:
            return
        removed = self.task_store.delete(task_id)
        await self._append(
            Notice(f"已删除任务 #{task_id}。" if removed else f"没有编号为 {task_id} 的任务。",
                   "info" if removed else "warn")
        )

    async def _toggle_task(self, payload: str, enabled: bool) -> None:
        task_id = self._parse_id(payload)
        if task_id is None:
            return
        ok = self.task_store.set_enabled(task_id, enabled)
        verb = "启用" if enabled else "停用"
        await self._append(
            Notice(f"已{verb}任务 #{task_id}。" if ok else f"没有编号为 {task_id} 的任务。",
                   "info" if ok else "warn")
        )

    async def _run_task_now(self, payload: str) -> None:
        task_id = self._parse_id(payload)
        if task_id is None:
            return
        task = self.task_store.get(task_id)
        if task is None:
            await self._append(Notice(f"没有编号为 {task_id} 的任务。", "warn"))
            return
        self.run_manual_task(task)

    def _parse_id(self, payload: str) -> int | None:
        try:
            return int(payload.strip().lstrip("#"))
        except ValueError:
            self.call_later(self._append, Notice(f"'{payload}' 不是有效的任务编号。", "warn"))
            return None

    # -------------------------------------------------------- scheduled runs
    async def _run_scheduled_task(self, task: ScheduledTask) -> None:
        if self._busy:
            await self._append(
                Notice(f"⏰ 任务 #{task.id} 到点，但当前正忙，本次已跳过。", "warn")
            )
            return
        await self._execute_task(task)

    @work(exclusive=True, group="task")
    async def run_manual_task(self, task: ScheduledTask) -> None:
        if self._busy:
            await self._append(Notice("当前有任务在跑，稍后再试。", "warn"))
            return
        await self._execute_task(task)

    async def _execute_task(self, task: ScheduledTask) -> None:
        await self._append(
            Notice(f"⏰ 定时任务 #{task.id}：{task.prompt}（{task.schedule_text}）", "info")
        )
        original = self.agent.confirm_handler
        self.agent.confirm_handler = self._task_confirm(task)

        async def events():
            async for event in self.agent.run(f"【定时任务 #{task.id}】{task.prompt}"):
                yield event

        answer_parts: list[str] = []
        try:
            async for event in self._stream_agent(events):
                if event["type"] == "text":
                    answer_parts.append(event["text"])
        finally:
            self.agent.confirm_handler = original

        answer = "".join(answer_parts).strip()
        if self._notify_enabled:
            await asyncio.to_thread(
                notify_mod.notify,
                f"JARVIS · {task.title[:40]}",
                " ".join(answer.split())[:180] or "(无输出)",
            )

    def _task_confirm(self, task: ScheduledTask):
        async def handler(request: ConfirmRequest) -> bool:
            if task.allow_dangerous:
                await self._append(Notice(f"任务已授权，自动同意：{request.summary}", "warn"))
                return True
            await self._append(
                Notice(f"任务未授权危险操作，已拒绝：{request.summary}（需要的话加 --danger）", "warn")
            )
            return False

        return handler

    # --------------------------------------------------------------- workers
    @work(exclusive=True, group="plan")
    async def run_plan_execute(self) -> None:
        await self._append(Notice("开始执行当前计划。", "info"))
        async for _event in self._stream_agent(self.agent.execute_plan):
            pass

    @work(exclusive=True, group="sys")
    async def run_sys_report(self) -> None:
        try:
            report = await asyncio.to_thread(sysinfo.sys_report, 8)
        except Exception as exc:  # noqa: BLE001
            await self._append(Notice(f"采集失败：{exc}", "bad"))
            return
        await self._append(Notice("系统体检\n" + report, "info"))

    # ------------------------------------------------------------------ hooks
    async def action_clear_chat(self) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        await chat.remove_children()
        await chat.mount(Banner(id="banner"))
        self._plan_view = None
        self._stream_widget = None
        self._thinking_widget = None
        self._tool_view = None

    def action_toggle_menu(self) -> None:
        """Collapse / expand the left command menu (Ctrl+B)."""

        menu = self.query_one("#menu")
        menu.toggle_class("collapsed")
        if menu.has_class("collapsed"):
            self._focus_prompt()
            return
        try:
            self.query_one("#menu-list", CommandMenu).focus()
        except Exception:  # noqa: BLE001 - never wedge the key on a missing widget
            self._focus_prompt()

    def _collapse_menu(self) -> None:
        self.query_one("#menu").add_class("collapsed")

    @on(MenuCommand)
    async def _on_menu_command(self, event: MenuCommand) -> None:
        """Run a menu row, then hand the caret back to the prompt.

        Selecting from the menu puts it away again: the chat is the normal state,
        and the command it runs is echoed like any other slash command.
        """

        event.stop()
        self._collapse_menu()
        self._focus_prompt()
        await self._run_command(event.command)

    @on(MenuDismiss)
    def _on_menu_dismiss(self, event: MenuDismiss) -> None:
        event.stop()
        self._collapse_menu()
        self._focus_prompt()

    def _side_hidden(self) -> bool:
        try:
            return self.query_one("#side").has_class("hidden")
        except Exception:  # noqa: BLE001 - during teardown the widget is gone
            return True

    def action_toggle_side(self) -> None:
        side = self.query_one("#side")
        side.toggle_class("hidden")
        if side.has_class("hidden"):
            self._focus_prompt()
            return
        # Sample on open: while the panel is collapsed its timers stay quiet, so
        # the numbers would otherwise be missing (or stale) on the first look.
        self._set_panel_text("正在采集系统状态…")
        self.run_worker(self._sample_panel(), group="panel-open", exclusive=True)

    async def _sample_panel(self) -> None:
        """Fetch both halves of the panel, cheap metrics first.

        One worker, not two racing: the process walk holds the shared psutil lock
        for seconds, so starting it alongside the metrics can delay exactly the
        numbers the user just opened the panel to see.
        """

        if self._panel_busy or self._top_busy:
            return
        self._top_busy = True
        try:
            await self._refresh_panel_async()
            if not self._side_hidden():
                await self._refresh_processes_async()
        finally:
            self._top_busy = False

    def _set_panel_text(self, text: str) -> None:
        try:
            self.query_one("#syspanel", Static).update(text)
        except Exception:  # noqa: BLE001 - cosmetic
            pass

    def action_cancel(self) -> None:
        if not self._busy:
            self.notify("当前没有正在执行的任务。")
            return
        for worker in self.workers:
            if worker.group in {"chat", "task", "plan"}:
                worker.cancel()

    def action_help(self) -> None:
        self.push_screen(HelpScreen(), callback=lambda _result: self._focus_prompt())

    async def action_tasks(self) -> None:
        await self._show_tasks()

    async def _append(self, widget) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        await chat.mount(widget)
        chat.scroll_end(animate=False)

    def _refresh_panel(self) -> None:
        """Timer tick: sample the cheap system metrics off the UI thread.

        Walking every process with psutil costs seconds on a busy machine, so
        the process table has its own slower timer (:meth:`_refresh_processes`).
        Both run in worker threads: a slow sample can never freeze the window.
        Both also stand down while the panel is collapsed - nobody can see the
        numbers, so sampling them is pure waste.
        """

        if self._side_hidden() or self._panel_busy:
            return
        self._panel_busy = True
        self.run_worker(self._refresh_panel_async(), group="panel", exclusive=True)

    async def _refresh_panel_async(self) -> None:
        try:
            data = await asyncio.to_thread(sysinfo.snapshot, 0)
        except Exception:  # noqa: BLE001 - panel is cosmetic, never crash the UI
            return
        finally:
            self._panel_busy = False
        self._panel_data = data
        self._render_panel()

    def _refresh_processes(self) -> None:
        """Timer tick: refresh the (expensive) TOP 进程 table.

        Yields to the cheap panel metrics: the process walk holds the shared
        psutil lock for seconds, and queueing it in front of the 2s metrics would
        stall the panel the user is looking at.
        """

        if self._side_hidden() or self._top_busy or self._panel_busy:
            return
        self._top_busy = True
        self.run_worker(self._refresh_processes_async(), group="top", exclusive=True)

    async def _refresh_processes_async(self) -> None:
        try:
            text = await asyncio.to_thread(sysinfo.top_processes, 5)
        except Exception:  # noqa: BLE001 - panel is cosmetic, never crash the UI
            return
        finally:
            self._top_busy = False
        self._top_text = text
        self._render_panel()

    # Sidebar label column, in cells. Wide enough for the longest label (a
    # three-glyph CJK word = 6 cells) plus a space, so every value in a section
    # starts on the same column instead of drifting with the label's width.
    SIDE_LABEL = 7

    def _panel_lines(self, width: int) -> list[str]:
        """Build the sidebar as fixed rows, every one clipped to ``width``.

        Nothing here may exceed ``width``: ``#side`` does not scroll, so an
        overlong line wraps and the column layout collapses.
        """

        data = self._panel_data or {}
        model = self.agent.model
        tasks = self.task_store.list()
        next_task = min(
            (task for task in tasks if task.enabled and task.next_run),
            key=lambda task: task.next_run,
            default=None,
        )

        def row(label: str, value: str) -> str:
            return clip(pad(label, self.SIDE_LABEL) + value, width)

        def fit(text: str) -> list[str]:
            return [clip(line, width) for line in str(text).splitlines()] or [""]

        lines = ["SYSTEM", clip(str(data.get("host", "")), width), ""]
        lines.append(row("CPU", str(data.get("cpu", ""))))
        lines += [
            clip(line, width)
            for line in sysinfo.core_rows(data.get("cores", []), indent=self.SIDE_LABEL)
        ]
        lines += [
            row("MEM", str(data.get("memory", ""))),
            row("SWAP", str(data.get("swap", ""))),
            row("NET", str(data.get("net", ""))),
            row("BATT", str(data.get("battery", ""))),
            "",
            "DISK",
        ]
        lines += fit(data.get("disks", ""))
        lines += ["", "TOP 进程"]
        lines += fit(self._top_text)
        lines += [
            "",
            "SESSION",
            row("模型", f"{model.display} · {model.model}"),
            row("来源", f"{model.provider or '自定义'} · {model.base_url.split('//')[-1]}"),
            row("工具", f"{len(self.registry.tools)} 个"),
            row(
                "搜索",
                self.config.search.provider + ("" if self.config.search.enabled else "（关闭）"),
            ),
            row("上下文", f"{len(self.agent.history())} 条消息"),
            row("记忆", f"{self.store.fact_count()} 条事实"),
            row("计划", f"{len(self.agent.plan)} 步" + (" (执行中)" if self._busy else "")),
            row(
                "任务",
                f"{len([task for task in tasks if task.enabled])} 个启用"
                + (f"，下次 {next_task.next_run[5:16]}" if next_task else ""),
            ),
            row("会话", self.session_id),
        ]
        return lines

    def _render_panel(self) -> None:
        try:
            panel = self.query_one("#syspanel", Static)
        except Exception:  # noqa: BLE001 - widget may already be gone
            return
        # Measure the real box rather than trusting the CSS constant: before the
        # first layout ``content_size`` is 0, so fall back to the fixed width.
        width = panel.content_size.width
        if not width or width < 20:
            width = sysinfo.PANEL_WIDTH
        panel.update("\n".join(self._panel_lines(width)))


# --------------------------------------------------------------------- selftest
def _selftest(ping: bool = False) -> int:
    """Headless sanity check: config, tools, registry, scheduler, optional live call."""

    from ..core.scheduler import ScheduledTask
    from ..tools import fs

    config = load_config()
    print(f"config      : {config.path} (created={config.created})")
    print(f"default     : {config.default_model}")
    registry = build_registry(config.security, str(config.workdir), config.search)
    print(f"tools       : {', '.join(t.name for t in registry.tools)}")
    print(f"workdir     : {config.workdir}")
    print(
        f"memory      : auto_learn={config.memory.auto_learn} every={config.memory.learn_every} "
        f"max_in_prompt={config.memory.max_facts_in_prompt}"
    )
    print(f"daemon      : hotkey={config.daemon.hotkey} scheduler={config.daemon.scheduler} "
          f"tray={config.daemon.tray}")
    print(f"providers   : {len(config.catalog.presets)} 个内置 + "
          f"{len(config.user_providers)} 个自定义；search={web.describe(config.search)}")
    for name in config.model_names():
        model = config.models[name]
        print(
            f"  model {name:<10} provider={model.provider or '-':<12} {model.model:<28} "
            f"key={'yes' if model.key_ready else 'MISSING'}"
        )
    print(f"fallback    : {' → '.join(config.fallbacks_for(config.default_model)) or '(无)'}")

    print("--- sysinfo ---")
    print(sysinfo.sys_report(include_processes=3))
    print("--- clipboard ---")
    preview = clipboard_mod.clipboard_text(80)
    print(f"text        : {preview or '(剪贴板没有文本)'}")
    print("--- processes ---")
    print(procman.list_processes(sort_by="cpu", limit=3))
    print("--- fs ---")
    print(fs.list_dir(str(config.workdir))[:400])

    store = HistoryStore(default_db_path())
    print(f"--- store ---\n{store.path} · facts={store.fact_count()} · sessions/messages={store.stats()}")
    task_store = TaskStore(store.conn)
    demo = ScheduledTask(id=None, kind="interval", spec="30", prompt="selftest")
    from datetime import datetime as _dt

    now = _dt(2026, 9, 11, 12, 0)
    print(
        f"--- scheduler ---\ninterval next={demo.next_after(now)} · "
        f"tasks={len(task_store.list())}"
    )
    store.close()

    model = config.model()
    key = model.resolve_api_key()
    print(f"--- model {model.name}: key={'yes' if key else 'MISSING'} base_url={model.base_url}")
    if ping and key:
        from ..llm.client import LLMClient

        async def _ping() -> None:
            client = LLMClient(model)
            result = await client.stream([{"role": "user", "content": "只回复两个字：在线"}])
            print(f"reply       : {result.content.strip()[:80]}")
            await client.aclose()

        asyncio.run(_ping())
    return 0
