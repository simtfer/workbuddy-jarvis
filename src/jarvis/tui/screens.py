"""Modal screens: dangerous-action confirmation and help."""

from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

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


class HelpScreen(ModalScreen[None]):
    """Keyboard and slash-command cheatsheet."""

    BINDINGS = [("escape", "close", "关闭"), ("f1", "close", "关闭")]

    CSS = """
    HelpScreen { align: center middle; }
    #help-box {
        width: 80%; max-width: 110; height: auto;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #help-body { color: $text; }
    """

    HELP_TEXT = """\
JARVIS-Win · Phase 1

对话
  直接输入问题，回车发送。JARVIS 会自行决定是否调用工具。
  Esc 中断正在生成的回答。

斜杠命令
  /help            显示本帮助
  /model [名字]    查看或切换模型（deepseek / qwen / doubao / ollama）
  /tools           列出可用工具
  /sys             立刻输出一份系统体检报告
  /history         查看已保存的历史会话
  /reset           清空当前对话上下文（历史仍保存在 data/ 中）
  /clear           清空屏幕
  /theme           切换深浅主题
  /quit            退出

快捷键
  Ctrl+Q 退出    Ctrl+L 清屏    Ctrl+S 显示/隐藏系统面板
  F1 帮助        Ctrl+X 取消当前任务

安全
  执行命令、写文件等破坏性操作都会弹窗确认；
  在 config.toml 的 security.auto_approve 里可加白名单（谨慎使用）。
"""

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(self.HELP_TEXT, id="help-body", markup=False)

    def action_close(self) -> None:
        self.dismiss(None)
