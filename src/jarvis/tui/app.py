"""The Textual application: JARVIS' face."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, Static

from .. import __version__
from ..config import DATA_DIR, Config, load_config
from ..core.agent import Agent, ConfirmRequest
from ..memory.store import HistoryStore
from ..tools import build_registry
from ..tools import sysinfo
from .screens import ConfirmScreen, HelpScreen
from .widgets import AssistantMessage, Notice, ToolCallView, ToolResultView, UserMessage

BANNER = r"""
   ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
   ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
   ██║███████║██████╔╝██║   ██║██║███████╗
██ ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝
"""


class JarvisApp(App[None]):
    """Windows resident assistant, phase 1."""

    TITLE = "JARVIS-Win"
    SUB_TITLE = f"v{__version__} · 说人话，也干活"

    CSS = """
    #body { height: 1fr; }
    #chat { width: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
    #side {
        width: 44; padding: 0 1; border-left: solid $panel; color: $text-muted;
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
    #prompt { dock: bottom; }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出"),
        Binding("ctrl+l", "clear_chat", "清屏"),
        Binding("ctrl+s", "toggle_side", "系统面板"),
        Binding("ctrl+x", "cancel", "取消任务"),
        Binding("f1", "help", "帮助"),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.store = HistoryStore(DATA_DIR / "history.db")
        self.session_id = self.store.new_session_id()
        self.store.ensure_session(self.session_id, config.default_model)
        self.registry = build_registry(config.security, str(config.workdir))
        self.agent = Agent(
            config=config,
            registry=self.registry,
            model_name=config.default_model,
            confirm_handler=self._confirm,
            session_id=self.session_id,
            on_message=self._persist,
        )
        self._session_allow: set[str] = set()
        self._stream_widget: AssistantMessage | None = None
        self._busy = False
        self._last_flush = 0.0
        self._tool_count = 0

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
        self.run_worker(self._greet(), name="greet")

    async def on_unmount(self) -> None:
        await self.agent.aclose()
        self.store.close()

    def _persist(self, message: dict[str, Any]) -> None:
        role = message.get("role")
        content = message.get("content")
        if role in {"user", "assistant"} and content:
            self.store.add_message(self.session_id, str(role), str(content))
            if role == "user":
                self.store.set_title(self.session_id, str(content).strip().replace("\n", " "))

    async def _greet(self) -> None:
        model = self.agent.model
        key_ok = bool(model.resolve_api_key()) or "localhost" in model.base_url or "127.0.0.1" in model.base_url
        await self._append(
            Notice(
                f"JARVIS 已就绪 · 模型 {model.display}（{model.model}） · "
                f"工具 {len(self.registry.tools)} 个 · 数据目录 {DATA_DIR}",
                "info",
            )
        )
        if self.config.created:
            await self._append(
                Notice(
                    f"已生成配置文件 {self.config.path}，请填写 API Key 后重启或换模型。",
                    "warn",
                )
            )
        if not key_ok:
            await self._append(
                Notice(
                    f"⚠ 模型 {model.display} 还没有 API Key："
                    f"在 config.toml 填入 api_key，或设置环境变量 {model.api_key_env or 'API_KEY'}。"
                    " 期间 /sys /tools 等本地命令仍可用。",
                    "bad",
                )
            )

        last = self.store.last_session()
        sessions, messages = self.store.stats()
        if last and last[0] != self.session_id:
            await self._append(
                Notice(
                    f"本地已存 {sessions} 个会话 / {messages} 条消息，"
                    f"最近一次：{last[0]}（{last[1] or '无标题'}）。用 /history 查看，/resume <id> 载入上下文。",
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
        self._busy = True
        self._tool_count = 0
        self._stream_widget = None
        started = time.monotonic()
        await self._append(UserMessage(text))
        try:
            async for event in self.agent.run(text):
                await self._handle_event(event)
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
        parts = raw.split()
        command = parts[0].lower()
        args = parts[1:]

        if command in {"/quit", "/exit", "/q"}:
            self.exit()
        elif command in {"/help", "/?"}:
            self.push_screen(HelpScreen())
        elif command == "/clear":
            await self.action_clear_chat()
        elif command == "/sys":
            self.run_sys_report()
        elif command == "/tools":
            lines = [f"· {tool.name:<14} {'⚠ 需确认' if tool.dangerous else '免确认'}  {tool.description[:60]}" for tool in self.registry.tools]
            await self._append(Notice("可用工具：\n" + "\n".join(lines), "info"))
        elif command == "/models" or (command == "/model" and not args):
            lines = []
            for name, model in sorted(self.config.models.items()):
                mark = "▶" if name == (self.agent.model_name or self.config.default_model) else " "
                key = "已配置 Key" if model.resolve_api_key() else "缺 Key"
                lines.append(f"{mark} {name:<10} {model.model:<28} {key}")
            await self._append(Notice("模型列表（/model <名字> 切换）：\n" + "\n".join(lines), "info"))
        elif command == "/model" and args:
            name = args[0]
            try:
                self.agent.set_model(name)
            except KeyError as exc:
                await self._append(Notice(str(exc.args[0]), "warn"))
                return
            await self._append(Notice(f"已切换到 {name} · {self.agent.model.model}", "info"))
        elif command == "/history":
            rows = self.store.sessions(15)
            if not rows:
                await self._append(Notice("还没有历史会话。", "info"))
                return
            lines = [f"{sid}  {title or '(无标题)'}" for sid, title, _ts in rows]
            await self._append(Notice("最近会话（/resume <id> 载入上下文）：\n" + "\n".join(lines), "info"))
        elif command == "/resume" and args:
            target = args[0]
            history = self.store.recent_messages(target, self.config.security.history_limit)
            if not history:
                await self._append(Notice(f"会话 {target} 没有可载入的消息。", "warn"))
                return
            self.agent.load_history(history)
            await self._append(
                Notice(f"已载入会话 {target} 的 {len(history)} 条消息作为上下文。", "info")
            )
        elif command == "/reset":
            self.agent.reset()
            await self._append(Notice("上下文已清空（历史仍保存在 data/history.db）。", "info"))
        elif command == "/theme":
            self.theme = "textual-light" if self.theme == "textual-dark" else "textual-dark"
            await self._append(Notice(f"主题：{self.theme}", "info"))
        else:
            await self._append(Notice(f"未知命令 {command}，/help 查看全部命令。", "warn"))

    # --------------------------------------------------------------- workers
    @work(exclusive=True, group="sys")
    async def run_sys_report(self) -> None:
        try:
            report = await asyncio.to_thread(sysinfo.sys_report, 8)
        except Exception as exc:  # noqa: BLE001
            await self._append(Notice(f"采集失败：{exc}", "bad"))
            return
        await self._append(Notice("系统体检\n" + report, "info"))

    def _refresh_panel(self) -> None:
        try:
            data = sysinfo.snapshot(include_processes=5)
        except Exception:  # noqa: BLE001 - panel is cosmetic, never crash the UI
            return
        model = self.agent.model
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
                    f"会话   {self.session_id}",
                ]
            )
        )

    # --------------------------------------------------------------- actions
    async def action_clear_chat(self) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        await chat.remove_children()
        await chat.mount(Static(BANNER, id="banner", markup=False))

    def action_toggle_side(self) -> None:
        self.query_one("#side").toggle_class("hidden")

    def action_cancel(self) -> None:
        if not self._busy:
            self.notify("当前没有正在执行的任务。")
            return
        for worker in self.workers:
            if worker.group == "chat":
                worker.cancel()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    async def _append(self, widget) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        await chat.mount(widget)
        chat.scroll_end(animate=False)


# --------------------------------------------------------------------- entry
def _selftest(ping: bool = False) -> int:
    """Headless sanity check: config, tools, registry, optional live call."""

    config = load_config()
    print(f"config      : {config.path} (created={config.created})")
    print(f"default     : {config.default_model}")
    registry = build_registry(config.security, str(config.workdir))
    print(f"tools       : {', '.join(t.name for t in registry.tools)}")
    print(f"workdir     : {config.workdir}")
    print("--- sysinfo ---")
    print(sysinfo.sys_report(include_processes=3))
    print("--- fs ---")
    from ..tools import fs

    print(fs.list_dir(str(config.workdir))[:400])

    model = config.model()
    key = model.resolve_api_key()
    print(f"--- model {model.name}: key={'yes' if key else 'MISSING'} base_url={model.base_url}")
    if ping and key:
        from ..llm.client import LLMClient

        async def _ping() -> None:
            client = LLMClient(model)
            result = await client.stream([{"role": "user", "content": "只回复两个字：在线"}])
            print(f"reply       : {result.content.strip()[:80]}")

        asyncio.run(_ping())
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS-Win · Windows 超级 AI 助手")
    parser.add_argument("--selftest", action="store_true", help="不开 TUI，自检配置与工具")
    parser.add_argument("--ping", action="store_true", help="自检时额外发一次真实模型请求")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    args = parser.parse_args()

    if args.selftest or args.ping:
        sys.exit(_selftest(ping=args.ping))

    config = load_config()
    app = JarvisApp(config)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
