"""The Textual application: JARVIS' face."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, Static

from .. import __version__
from ..config import Config, load_config
from ..core.agent import Agent, ConfirmRequest
from ..core.scheduler import ScheduledTask, Scheduler, ScheduleError, TaskStore, parse_task_command
from ..memory.store import HistoryStore, default_db_path
from ..tools import build_registry, sysinfo
from ..tools import notify as notify_mod
from .screens import ConfirmScreen, HelpScreen
from .widgets import (
    AssistantMessage,
    Notice,
    PlanView,
    ToolCallView,
    ToolResultView,
    UserMessage,
)

BANNER = r"""
   ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
   ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
   ██║███████║██████╔╝██║   ██║██║███████╗
██ ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝
"""


class JarvisApp(App[None]):
    """Windows resident assistant."""

    TITLE = "JARVIS-Win"
    SUB_TITLE = f"v{__version__} · 说人话，也干活"

    CSS = """
    #body { height: 1fr; }
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
    .tool-call { color: $warning; padding: 0 1; margin-top: 1; }
    .tool-result { color: $text-muted; padding: 0 1 0 3; }
    .tool-result.bad { color: $error; }
    .notice { color: $text-muted; padding: 0 1; margin-top: 1; }
    .notice.bad { color: $error; }
    .notice.warn { color: $warning; }
    .plan-view {
        color: $accent; border: round $accent 40%; padding: 0 1; margin-top: 1;
    }
    #prompt { dock: bottom; }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出"),
        Binding("ctrl+l", "clear_chat", "清屏"),
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
        self.registry = build_registry(config.security, str(config.workdir))
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
        self._plan_view: PlanView | None = None
        self._busy = False
        self._last_flush = 0.0
        self._tool_count = 0
        self._turns_since_learn = 0
        self._notify_enabled = config.daemon.notify

    # ---------------------------------------------------------------- lifecycle
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with VerticalScroll(id="chat"):
                yield Static(BANNER, id="banner", markup=False)
            with Vertical(id="side"):
                yield Static("正在采集系统状态…", id="syspanel", markup=False)
        yield Input(placeholder="问我任何事，或输入 /help 查看命令", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "textual-dark"
        self._refresh_panel()
        self.set_interval(2.0, self._refresh_panel)
        if self.scheduler is not None:
            self.scheduler.on_error = self._on_schedule_error
            self.scheduler.start()
        self.run_worker(self._greet(), name="greet")

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
                f"工具 {len(self.registry.tools)} 个 · 记忆 {self.store.fact_count()} 条",
                "info",
            )
        )
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
            elapsed = time.monotonic() - started
            if self._tool_count:
                await self._append(
                    Notice(f"· 本次调用 {self._tool_count} 个工具，用时 {elapsed:.1f}s", "info")
                )

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "text":
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
            self._tool_count += 1
            args = event.get("arguments") or {}
            detail = str(args.get("command") or args.get("path") or "")[:90]
            await self._append(ToolCallView(event["name"], detail))
        elif kind == "tool_result":
            self._stream_widget = None
            await self._append(
                ToolResultView(event["name"], event["output"], bool(event.get("ok")))
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
        elif kind == "error":
            self._stream_widget = None
            await self._append(Notice(event["message"], "bad"))
        elif kind == "done":
            if self._stream_widget is not None:
                await self._stream_widget.append_text("")

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
            self.push_screen(HelpScreen())
        elif command == "/clear":
            await self.action_clear_chat()
        elif command == "/sys":
            self.run_sys_report()
        elif command == "/tools":
            lines = [
                f"· {tool.name:<14} {'⚠ 需确认' if tool.dangerous else '免确认'}  {tool.description[:60]}"
                for tool in self.registry.tools
            ]
            await self._append(Notice("可用工具：\n" + "\n".join(lines), "info"))
        elif command in {"/model", "/models"} and not rest:
            await self._show_models()
        elif command == "/model" and rest:
            try:
                self.agent.set_model(args[0])
            except KeyError as exc:
                await self._append(Notice(str(exc.args[0]), "warn"))
                return
            await self._append(Notice(f"已切换到 {args[0]} · {self.agent.model.model}", "info"))
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
            await self._append(
                Notice(
                    "常驻守护：`uv run jarvis --daemon`（全局热键 "
                    f"{hotkey} 唤起新窗口，并在后台跑定时任务）。\n"
                    "当前窗口若要接管调度器，直接启动 TUI 即可（不加 --no-scheduler）。",
                    "info",
                )
            )
        else:
            await self._append(Notice(f"未知命令 {command}，/help 查看全部命令。", "warn"))

    async def _show_models(self) -> None:
        lines = []
        for name, model in sorted(self.config.models.items()):
            mark = "▶" if name == (self.agent.model_name or self.config.default_model) else " "
            key = "已配置 Key" if model.resolve_api_key() else "缺 Key"
            lines.append(f"{mark} {name:<10} {model.model:<28} {key}")
        await self._append(Notice("模型列表（/model <名字> 切换）：\n" + "\n".join(lines), "info"))

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
        await chat.mount(Static(BANNER, id="banner", markup=False))
        self._plan_view = None
        self._stream_widget = None

    def action_toggle_side(self) -> None:
        self.query_one("#side").toggle_class("hidden")

    def action_cancel(self) -> None:
        if not self._busy:
            self.notify("当前没有正在执行的任务。")
            return
        for worker in self.workers:
            if worker.group in {"chat", "task", "plan"}:
                worker.cancel()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    async def action_tasks(self) -> None:
        await self._show_tasks()

    async def _append(self, widget) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        await chat.mount(widget)
        chat.scroll_end(animate=False)

    def _refresh_panel(self) -> None:
        try:
            data = sysinfo.snapshot(include_processes=5)
        except Exception:  # noqa: BLE001 - panel is cosmetic, never crash the UI
            return
        model = self.agent.model
        tasks = self.task_store.list()
        next_task = min((t for t in tasks if t.enabled and t.next_run), key=lambda t: t.next_run, default=None)
        panel = self.query_one("#syspanel", Static)
        panel.update(
            "\n".join(
                [
                    "SYSTEM",
                    data["host"],
                    "",
                    f"CPU   {data['cpu']}",
                    f"      {data['per_cpu']}",
                    f"MEM   {data['memory']}",
                    f"SWAP  {data['swap']}",
                    f"NET   {data['net']}",
                    f"BATT  {data['battery']}",
                    "",
                    "DISK",
                    data["disks"],
                    "",
                    "TOP 进程",
                    data["top"],
                    "",
                    "SESSION",
                    f"模型   {model.display} · {model.model}",
                    f"工具   {len(self.registry.tools)} 个",
                    f"上下文 {len(self.agent.history())} 条消息",
                    f"记忆   {self.store.fact_count()} 条事实",
                    f"计划   {len(self.agent.plan)} 步" + (" (执行中)" if self._busy else ""),
                    f"任务   {len([t for t in tasks if t.enabled])} 个启用"
                    + (f"，下次 {next_task.next_run[5:16]}" if next_task else ""),
                    f"会话   {self.session_id}",
                ]
            )
        )


# --------------------------------------------------------------------- selftest
def _selftest(ping: bool = False) -> int:
    """Headless sanity check: config, tools, registry, scheduler, optional live call."""

    from ..core.scheduler import ScheduledTask
    from ..tools import fs

    config = load_config()
    print(f"config      : {config.path} (created={config.created})")
    print(f"default     : {config.default_model}")
    registry = build_registry(config.security, str(config.workdir))
    print(f"tools       : {', '.join(t.name for t in registry.tools)}")
    print(f"workdir     : {config.workdir}")
    print(
        f"memory      : auto_learn={config.memory.auto_learn} every={config.memory.learn_every} "
        f"max_in_prompt={config.memory.max_facts_in_prompt}"
    )
    print(f"daemon      : hotkey={config.daemon.hotkey} scheduler={config.daemon.scheduler}")

    print("--- sysinfo ---")
    print(sysinfo.sys_report(include_processes=3))
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
