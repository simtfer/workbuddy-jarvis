"""The Textual application: JARVIS' face.

The App owns the chat-stream machinery (turn lifecycle, event rendering,
confirmation prompts) and the layout. Everything else lives next door:

- ``commands.py``   every ``/command`` handler (``CommandMixin``)
- ``syspanel.py``   right-hand system sidebar sampling + rendering
- ``styles.py``     the stylesheet
- ``screens.py`` / ``widgets.py``   modals and custom widgets
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.widgets import Header, Input, Static

from .. import __version__
from ..config import Config
from ..core.agent import Agent, ConfirmRequest
from ..core.scheduler import ScheduledTask, Scheduler, TaskStore
from ..memory.store import HistoryStore, default_db_path
from ..tools import build_registry, sysinfo
from ..tools import notify as notify_mod
from ..tools import web
from .commands import CommandMixin, split_flags
from .screens import ConfirmScreen, HelpScreen, SubAgentDetailScreen
from .styles import CSS
from .syspanel import SystemPanel
from .widgets import (
    AssistantMessage,
    Banner,
    CommandMenu,
    MenuCommand,
    MenuDismiss,
    Notice,
    PlanView,
    SubAgentBoard,
    SubAgentOpen,
    ThinkingView,
    ToolCallView,
    UserMessage,
    menu_head,
)

# The collapsed right panel's handle: an arrow pointing at where the panel
# will appear, kept ASCII-adjacent so it renders in any Windows terminal.
SIDE_RAIL = "◀"

__all__ = ["JarvisApp", "split_flags"]

# ``split_flags`` lives in commands.py (imported above); re-exported because
# tests (and muscle memory) import it from here.


# figlet "ansi_shadow" of JARVIS lives in widgets.py with the Banner widget that
# falls back to a compact wordmark when the chat pane is too narrow for it.


class JarvisApp(CommandMixin, App[None]):
    """Windows resident assistant."""

    TITLE = "JARVIS-Win"
    SUB_TITLE = f"v{__version__} · 说人话，也干活"

    # Focus the prompt box on boot. The chat pane is a scrollable container and
    # therefore focusable, so leaving this to the default auto-focus can park the
    # caret somewhere the user cannot type.
    AUTO_FOCUS = "#prompt"

    CSS = CSS

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
        self.panel = SystemPanel(self)

        self._session_allow: set[str] = set()
        self._stream_widget: AssistantMessage | None = None
        self._thinking_widget: ThinkingView | None = None
        self._tool_view: ToolCallView | None = None
        self._subagent_board: SubAgentBoard | None = None
        self._plan_view: PlanView | None = None
        self._busy = False
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
                yield Static("系统面板 · 点击收起", id="side-head", markup=False)
                yield Static("系统面板已收起 · Ctrl+S 打开", id="syspanel", markup=False)
            # Click target for the collapsed panel: #side is display:none, so
            # the handle has to live outside it.
            yield Static(SIDE_RAIL, id="side-rail", markup=False)
        yield Input(placeholder="问我任何事，或输入 /help（Ctrl+B 打开命令菜单）", id="prompt")

    def on_mount(self) -> None:
        self.theme = "textual-dark"
        self._focus_prompt()
        self.set_interval(2.0, self.panel.tick_metrics)
        # Walking every process is much more expensive than the metrics above,
        # so the TOP 进程 table refreshes on its own, slower clock.
        self.set_interval(10.0, self.panel.tick_processes)
        if not self.panel.side_hidden():
            self.panel.tick_metrics()
            self.panel.tick_processes()
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
        self._subagent_board = None
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
            self._subagent_board = None
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
            board = SubAgentBoard(prompts)
            self._subagent_board = board
            await self._append(board)
        elif kind == "subagent_done":
            if self._subagent_board is not None:
                self._subagent_board.update_child(event)
        elif kind == "subagent_timeout":
            if self._subagent_board is not None:
                self._subagent_board.update_all(event.get("results") or [])
            await self._append(
                Notice(
                    f"⏱ {event.get('default_timeout', 0):.0f}s 未跑完：已先输出部分结果，"
                    f"剩余子任务继续，完成后自动追加最终汇总。",
                    "warn",
                )
            )
        elif kind == "subagent_final":
            if self._subagent_board is not None:
                self._subagent_board.update_all(event.get("results") or [])
        elif kind == "subagent_summary":
            # Rendered as part of the tool_result that follows; the board
            # above already holds the per-child detail views.
            self._subagent_board = None
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
        self._subagent_board = None

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

    @on(SubAgentOpen)
    def _on_subagent_open(self, event: SubAgentOpen) -> None:
        """A board row was clicked: show that child's full transcript."""

        event.stop()
        self.push_screen(SubAgentDetailScreen(event.child))

    def action_toggle_side(self) -> None:
        if not self.panel.toggle():
            self._focus_prompt()

    # ------------------------------------------------------------ click to open
    # Both sidebars can be opened and closed with the mouse: each one has a
    # handle that is only on screen in the state where clicking it makes sense
    # (the rail when closed, the header when open), so one click never means
    # two things.
    @on(Click, "#menu-rail")
    def _on_menu_rail_click(self, event: Click) -> None:
        self.action_toggle_menu()

    @on(Click, "#menu-head")
    def _on_menu_head_click(self, event: Click) -> None:
        self._collapse_menu()

    @on(Click, "#side-rail")
    def _on_side_rail_click(self, event: Click) -> None:
        self.action_toggle_side()

    @on(Click, "#side-head")
    def _on_side_head_click(self, event: Click) -> None:
        if not self.panel.side_hidden():
            self.action_toggle_side()

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
