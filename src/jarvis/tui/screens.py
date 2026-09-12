"""Modal screens: dangerous-action confirmation and help."""

from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from ..core.agent import ConfirmRequest

ConfirmChoice = Literal["approve", "deny", "always"]


class ConfirmScreen(ModalScreen[ConfirmChoice]):
    """Ask the human before a destructive tool call runs."""

    BINDINGS = [
        ("escape", "deny", "拒绝"),
        ("y", "approve", "同意"),
        ("n", "deny", "拒绝"),
    ]

    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-box {
        width: 78%; max-width: 100; height: auto;
        border: round $warning; background: $surface; padding: 1 2;
    }
    #confirm-title { text-style: bold; color: $warning; margin-bottom: 1; }
    #confirm-tool { color: $text-muted; }
    #confirm-args { color: $text; padding: 0 0 1 0; }
    #confirm-hint { color: $text-muted; padding-bottom: 1; }
    #confirm-buttons { height: auto; align-horizontal: right; }
    #confirm-buttons Button { margin-left: 1; }
    """

    def __init__(self, request: ConfirmRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        with Container(id="confirm-box"):
            yield Static("⚠ 需要你的授权", id="confirm-title")
            yield Static(f"工具: {self.request.tool}", id="confirm-tool", markup=False)
            yield Static(self.request.summary, id="confirm-args", markup=False)
            hint = self.request.hint or "该操作会修改本机状态"
            yield Static(f"说明: {hint}   (y 同意 / n 拒绝 / Esc 关闭)", id="confirm-hint", markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Button("拒绝", id="deny")
                yield Button("本会话始终允许", id="always")
                yield Button("同意执行", id="approve", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        mapping = {"approve": "approve", "deny": "deny", "always": "always"}
        self.dismiss(mapping.get(event.button.id or "", "deny"))  # type: ignore[arg-type]

    def action_approve(self) -> None:
        self.dismiss("approve")

    def action_deny(self) -> None:
        self.dismiss("deny")


class ModelPickerScreen(ModalScreen[str | None]):
    """Pick a model with the arrow keys, then Enter.

    ``/model`` with no argument opens this instead of dumping a text list:
    switching models is a thing you do with your hands, not by retyping a name.
    Returns the chosen model's key, or ``None`` when cancelled.
    """

    BINDINGS = [("escape", "cancel", "取消")]

    CSS = """
    ModelPickerScreen { align: center middle; }
    #picker-box {
        width: 90%; max-width: 108; height: auto;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #picker-title { text-style: bold; color: $accent; }
    #picker-list { height: auto; max-height: 16; margin: 1 0; }
    #picker-hint { color: $text-muted; }
    """

    def __init__(self, choices: list[tuple[str, str]], current: str) -> None:
        super().__init__()
        self.choices = choices
        self.current_key = current

    def compose(self) -> ComposeResult:
        with Container(id="picker-box"):
            yield Static("选择模型", id="picker-title")
            yield OptionList(
                *(Option(label, id=key) for key, label in self.choices),
                id="picker-list",
            )
            yield Static(
                "↑↓ 移动 · Enter 切换 · Esc 取消　——　切换会同时设为默认模型，下次启动仍用它。",
                id="picker-hint",
            )

    def on_mount(self) -> None:
        listing = self.query_one("#picker-list", OptionList)
        # Start on the model in use so Enter is a no-op rather than a surprise.
        listing.highlighted = next(
            (index for index, (key, _) in enumerate(self.choices) if key == self.current_key),
            0,
        )
        listing.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Keyboard and slash-command cheatsheet."""

    BINDINGS = [("escape", "close", "关闭"), ("f1", "close", "关闭")]

    CSS = """
    HelpScreen { align: center middle; }
    #help-box {
        width: 80%; max-width: 110; height: 85%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #help-body { color: $text; }
    """

    HELP_TEXT = """\
JARVIS-Win · Phase 4

对话
  直接输入问题，回车发送。JARVIS 会自行决定是否调用工具。
  Esc 中断正在生成的回答。

界面
  Ctrl+B        收起 / 展开左侧命令菜单（↑↓ 选择，Enter 执行，Esc 收起）
  Ctrl+S        收起 / 展开右侧系统面板
  左侧菜单      帮助、清屏、历史、模型、工具、任务、记忆、主题、退出都在里面
  —— 两个侧栏默认都是收起的，聊天区先占满；面板收起时不做进程采样。
  —— 工具调用和思考过程都折叠成一行概览（✓/✗ ⚙ 工具名 + 关键参数），
     点击标题展开可看完整参数与输出。

剪贴板与进程
  /clip                      看看现在剪贴板里有什么（文本/文件列表/格式）
  /clip set <文本>           把这段文本放进剪贴板，之后直接 Ctrl+V
  /clip clear                清空剪贴板
  /ps                        占用 CPU 最高的 15 个进程
  /ps mem                    按内存排序；/ps chrome 只看名字含 chrome 的
  也可以用说的：「总结一下我复制的内容」「关掉占用最高的那个进程」
  —— 读写剪贴板、结束/挂起进程都会弹窗确认；系统关键进程会被拒绝。

模型与 Provider
  /model                     打开模型选择器（↑↓ 选，Enter 切换）
  /model list                打印模型表（带序号 / provider / Key 状态）
  /model 2                   按序号热切换；/model qwen 按名字切
  /model add gpt --provider openai --model gpt-4o-mini --env OPENAI_API_KEY
                             新增模型并写回 config.toml（加 --default 设为默认）
  /model rm <名字>           删除模型
  /model default <名字>      设为默认模型（/model 切换时也会自动记住）
  /model fallback qwen ollama  设置故障自动切换链
  /provider list             列出全部内置 provider + 搜索后端
  /provider add myvllm http://10.0.0.5:8000/v1 --env MYVLLM_KEY
                             接入任意 OpenAI 兼容的自建端点
  /provider info <名字>      看某个 provider 的详情

联网
  /search 关键词             直接搜一次（默认 DuckDuckGo，免 Key）
  /search backend bocha      换搜索后端：bocha / tavily / serper / searxng
  /fetch example.com         打开网页并抽出正文
  也可以直接说「搜一下 xxx」，JARVIS 会自己调 web_search / fetch_url。

基础命令
  /help            显示本帮助
  /tools           列出可用工具
  /sys             立刻输出一份系统体检报告
  /history         查看已保存的历史会话
  /resume <id>     载入某个历史会话作为上下文
  /reset           清空当前对话上下文（历史仍保存在 data/ 中）
  /clear           清空屏幕
  /theme           切换深浅主题
  /quit            退出

长期记忆
  /facts                 查看已沉淀的事实
  /remember <内容>       手动记住一条（例如「我习惯用 PowerShell」）
  /forget <编号>         删除某条记忆
  /learn                 立刻从最近对话里提炼记忆

计划模式
  /plan <任务>     让模型拆成 3-8 步计划
  /do              按当前计划逐步执行（每步都会显示进度）
  /auto <任务>     生成计划后立刻执行

定时任务
  /task list                       查看任务（F2 同效）
  /task add <内容> @every 30m      每 30 分钟跑一次
  /task add <内容> @daily 09:00    每天 09:00 跑
  /task add <内容> @once 2026-09-12 08:00
  /task add <内容> @daily 09:00 --danger   允许该任务执行写操作
  /task on|off <id> · /task rm <id> · /task run <id>

多任务与并发
  主 Agent 会在用户的问题天然可拆成 N 个互不依赖子任务时，自动调用
  delegate_subagents 并行派给子 Agent（每个独立对话、共享工具与数据库）。
  超时（默认 30s）后先把已完成的部分输出，剩下的继续跑完再追加最终汇总。

思考过程
  推理模型（DeepSeek-R1 等）输出的思考流会显示在回答上方的
  「🧠 思考过程」折叠块里——默认收起只占一行，点击标题即可展开；
  每一轮工具调用各有一块。普通模型没有这个流，界面不受影响。
  配置在 config.toml 的 [subagents]：default_timeout / max_runtime /
  max_concurrent / max_per_call。

常驻模式
  另开一个终端运行：uv run jarvis --daemon
  · Ctrl+Alt+J 唤起 JARVIS 窗口（热键在 config.toml 的 daemon.hotkey 里改）
  · 系统托盘出现 JARVIS 图标，右键可打开 / 看任务 / 退出
  · 后台按点跑定时任务，跑完弹通知

快捷键
  Ctrl+Q 退出    Ctrl+L 清屏    Ctrl+B 命令菜单    Ctrl+S 系统面板
  F1 帮助        F2 任务列表    Ctrl+X 取消当前任务

安全
  执行命令、写文件等破坏性操作都会弹窗确认；
  定时任务默认拒绝危险操作，除非创建时加了 --danger。
"""

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            with VerticalScroll(id="help-body"):
                yield Static(self.HELP_TEXT, markup=False)

    def action_close(self) -> None:
        self.dismiss(None)
