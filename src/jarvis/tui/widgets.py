"""Custom widgets for the JARVIS chat surface."""

from __future__ import annotations

from textual.widgets import Markdown, Static


class UserMessage(Static):
    """A message typed by the human."""

    def __init__(self, text: str) -> None:
        super().__init__(f"› {text}", markup=False, classes="user-message")


class AssistantMessage(Markdown):
    """Streaming markdown bubble for the assistant."""

    def __init__(self) -> None:
        super().__init__("", classes="assistant-message")
        self.buffer = ""

    async def append_text(self, text: str) -> None:
        self.buffer += text
        await self.update(self.buffer or "…")


class ToolCallView(Static):
    """One line telling the user which tool ran."""

    def __init__(self, name: str, detail: str) -> None:
        super().__init__(f"⚙ {name}  {detail}", markup=False, classes="tool-call")


class ToolResultView(Static):
    """Collapsed tool output (first lines only)."""

    def __init__(self, name: str, output: str, ok: bool, limit: int = 12) -> None:
        lines = output.splitlines()
        shown = "\n".join(lines[:limit])
        if len(lines) > limit:
            shown += f"\n… 还有 {len(lines) - limit} 行（已省略）"
        prefix = "✓" if ok else "✗"
        super().__init__(f"{prefix} {name}\n{shown}", markup=False, classes="tool-result ok" if ok else "tool-result bad")


class Notice(Static):
    """System / status line inside the chat stream."""

    def __init__(self, text: str, kind: str = "info") -> None:
        super().__init__(text, markup=False, classes=f"notice {kind}")
