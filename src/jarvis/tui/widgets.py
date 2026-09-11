"""Custom widgets for the JARVIS chat surface."""

from __future__ import annotations

from typing import Any

from textual.widgets import Markdown, Static

MARKS = {
    "pending": "·",
    "running": "▶",
    "done": "✓",
    "failed": "✗",
    "skipped": "-",
}


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


class PlanView(Static):
    """Live view of a plan and its step statuses.

    Note: attribute names must avoid ``task``/``name`` etc. - Widget already
    defines those, and assigning them from a subclass raises.
    """

    def __init__(self, goal: str, steps: list[dict[str, Any]]) -> None:
        super().__init__("", markup=False, classes="plan-view")
        self.plan_goal = goal
        self.plan_steps = [dict(step) for step in steps]
        self.update(self.render_plan())

    def render_plan(self) -> str:
        total = len(self.plan_steps)
        lines = [f"计划 · {self.plan_goal}"]
        for index, step in enumerate(self.plan_steps, start=1):
            mark = MARKS.get(str(step.get("status", "pending")), "·")
            lines.append(f"  {mark} {index}/{total}  {step.get('title', '')}")
            detail = str(step.get("detail") or "").strip()
            if detail and step.get("status") == "running":
                lines.append(f"        {detail}")
        return "\n".join(lines)

    def set_status(self, index: int, status: str) -> None:
        if 0 <= index < len(self.plan_steps):
            self.plan_steps[index]["status"] = status
            self.update(self.render_plan())

