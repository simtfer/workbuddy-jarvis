"""Custom widgets for the JARVIS chat surface."""

from __future__ import annotations

import json
from typing import Any

from textual import events, on
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Collapsible, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from ..textwidth import clip, dwidth, pad

MARKS = {
    "pending": "·",
    "running": "▶",
    "done": "✓",
    "failed": "✗",
    "skipped": "-",
}

# figlet "ansi_shadow" of JARVIS. Every line is exactly 44 cells wide and the
# glyphs only line up at that exact shape - re-indenting or re-wrapping it makes
# the "J" drift away from its own bowl, which is what a crooked banner looks like.
BANNER = r"""
     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
     ██║███████║██████╔╝██║   ██║██║███████╗
██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝
""".strip("\n")

BANNER_WIDTH = dwidth(BANNER.splitlines()[0])

# Fallback for a chat pane too narrow for the art: with both sidebars open on a
# 120-column terminal the pane is 42 cells, and the 44-cell art would wrap and
# turn into noise. One line, well under that.
BANNER_COMPACT = "J A R V I S  ·  说人话，也干活"


class Banner(Static):
    """The JARVIS wordmark, downgraded to a compact form when the pane is narrow."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(BANNER, markup=False, **kwargs)
        self._compact = False

    def on_resize(self, event: events.Resize) -> None:
        compact = event.size.width < BANNER_WIDTH
        if compact != self._compact:
            self._compact = compact
            self.update(BANNER_COMPACT if compact else BANNER)


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


class ThinkingView(Collapsible):
    """The model's chain of thought, tucked away behind a collapsed header.

    Reasoning models (DeepSeek-R1 and friends) stream their thinking before the
    answer. It is genuinely useful on demand and pure noise at a glance, so the
    block starts collapsed; click the title (or focus + Enter) to expand.

    Note: do NOT override ``compose`` here - ``Collapsible.compose`` yields the
    title row plus a ``Contents`` wrapper, and replacing it would drop the
    toggle header entirely. The body is passed in as a constructor child so it
    lands inside ``Contents`` automatically.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._body = Static("", markup=False, classes="thinking-body")
        super().__init__(
            self._body,
            title="思考过程",
            collapsed=True,
            classes="thinking",
            **kwargs,
        )
        self.buffer = ""

    def append_text(self, text: str) -> None:
        """Accumulate reasoning deltas. Cheap: the body is hidden while collapsed."""

        self.buffer += text
        self._body.update(self.buffer)

    def finish(self) -> None:
        """Stamp the final length onto the title once the round is over."""

        chars = len(self.buffer)
        if chars:
            self.title = f"思考过程（{chars} 字）"


TOOL_RESULT_LIMIT = 12  # lines of tool output kept inside the collapsed body


class ToolCallView(Collapsible):
    """One tool call, collapsed to a single overview line.

    The title is the overview - live status mark, tool name, and its most
    telling argument - so a glance at the chat shows what happened without
    the wall of JSON. Expanding reveals the full arguments and a trimmed
    copy of the output. Like :class:`ThinkingView`, the body rides in as a
    constructor child (never override ``compose`` on a Collapsible subclass).
    """

    def __init__(
        self,
        name: str,
        detail: str = "",
        arguments: dict[str, Any] | None = None,
        output: str = "",
        ok: bool = True,
        **kwargs: Any,
    ) -> None:
        self._body = Static("", markup=False, classes="tool-body")
        super().__init__(
            self._body, title="", collapsed=True, classes="tool-call", **kwargs
        )
        self.tool_name = name
        self._detail = detail
        self._args_line = self._render_arguments(arguments)
        self._status = "running"
        self._output = ""
        self._refresh_view()
        if output:
            self.set_result(output, ok)

    @staticmethod
    def _render_arguments(arguments: dict[str, Any] | None) -> str:
        if not arguments:
            return ""
        try:
            pretty = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            pretty = str(arguments)
        return f"参数  {pretty}"

    # NOTE: named ``_refresh_view``, not ``_render`` - ``Widget._render`` is
    # Textual's internal line renderer and overriding it returns None into
    # ``Visual.to_strips`` and crashes rendering (same trap as ``task``/``name``).
    def _refresh_view(self) -> None:
        mark = {"running": "…", "ok": "✓", "bad": "✗"}.get(self._status, "·")
        title = f"{mark} ⚙ {self.tool_name}"
        if self._detail:
            title += f"  {self._detail}"
        summary = self._output_summary()
        if summary:
            # One-line answer of the call, right in the overview: most tool
            # results (echo, search hit count, ok/fail) are readable at a
            # glance without expanding anything.
            title += f"  →  {summary}"
        self.title = title
        self.set_class(self._status == "bad", "bad")

        lines: list[str] = []
        if self._args_line:
            lines.append(self._args_line)
        if self._output:
            body_lines = self._output.splitlines()
            kept = "\n".join(body_lines[:TOOL_RESULT_LIMIT])
            if len(body_lines) > TOOL_RESULT_LIMIT:
                kept += f"\n… 还有 {len(body_lines) - TOOL_RESULT_LIMIT} 行（已省略）"
            lines.append("结果\n" + kept)
        self._body.update("\n".join(lines))

    def _output_summary(self) -> str:
        """First meaningful line of the output, clipped to overview width."""

        if not self._output:
            return ""
        for line in self._output.splitlines():
            stripped = line.strip()
            if stripped:
                return clip(stripped, 48)
        return ""

    def set_result(self, output: str, ok: bool) -> None:
        """Record the tool result and re-render in finished form."""

        self._output = output
        self._status = "ok" if ok else "bad"
        self._refresh_view()


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


# ------------------------------------------------------------------ left menu
# Geometry in cells. ``#menu`` is ``MENU_WIDTH`` wide; subtract its 1-cell
# horizontal padding and 1-cell right border to get the usable width. The CSS in
# ``JarvisApp`` splices these numbers in, and ``tests/smoke_tui.py`` measures the
# real widget, so the constant and the box cannot drift apart silently.
MENU_WIDTH = 30
MENU_CONTENT = 27
MENU_RAIL_WIDTH = 4
MENU_LABEL = 12  # label column; the "what you could type instead" hint sits after it

# The sidebar's contents, in order: (section, [entry, ...]) where an entry is
# ``(label, command)`` or ``(label, command, hint)``.
#
# ``command`` is what the row runs; ``hint`` is the right-hand column, which
# defaults to the command itself (so a key-bound row can show ``F1`` while still
# running ``/help``). Keep labels inside ``MENU_LABEL`` and the widest row inside
# ``MENU_CONTENT`` - the smoke test fails on a row that would be clipped.
MenuEntry = tuple[str, str] | tuple[str, str, str]
MENU: list[tuple[str, list[MenuEntry]]] = [
    (
        "会话",
        [
            ("帮助与命令", "/help", "F1"),
            ("清屏对话", "/clear", "Ctrl+L"),
            ("历史会话", "/history"),
            ("重置上下文", "/reset"),
        ],
    ),
    (
        "模型",
        [
            ("切换模型…", "/model"),
            ("模型列表", "/model list"),
            ("服务商列表", "/provider list"),
        ],
    ),
    (
        "工具",
        [
            ("系统体检", "/sys"),
            ("进程管理", "/ps"),
            ("剪贴板", "/clip"),
            ("工具清单", "/tools"),
        ],
    ),
    (
        "任务",
        [
            ("定时任务", "/task list"),
        ],
    ),
    (
        "记忆",
        [
            ("长期记忆", "/facts"),
            ("提炼记忆", "/learn"),
        ],
    ),
    (
        "其他",
        [
            ("切换主题", "/theme"),
            ("退出", "/quit", "Ctrl+Q"),
        ],
    ),
]


def menu_head() -> str:
    """The hint block above the menu: the sidebar replaced the key-hint footer."""

    return "\n".join(
        [
            "菜单 · Ctrl+B 收起",
            "↑↓ 选择 · Enter 执行",
            "F1 帮助 · F2 任务",
            "Ctrl+S 面板 · Ctrl+Q 退出",
        ]
    )


def _section_row(name: str, width: int) -> str:
    """``── 会话 ─────`` filled out to the label column (never past ``width``)."""

    head = f"── {name} "
    return clip(head + "─" * max(0, MENU_LABEL - dwidth(head)), width)


class MenuCommand(Message):
    """A menu row was activated; ``command`` is the slash command to run."""

    def __init__(self, command: str) -> None:
        self.command = command
        super().__init__()


class MenuDismiss(Message):
    """The user asked the menu to go away again (Esc)."""


class CommandMenu(OptionList):
    """The left sidebar: a keyboard-navigable menu of commands.

    Rows are aligned by *cell* width, not character count, because a CJK label is
    twice as wide as an ASCII one - hence ``markup=False`` (so ``[`` in a row is
    literal) and the padding helpers from :mod:`jarvis.textwidth`.
    """

    BINDINGS = [Binding("escape", "dismiss_menu", "收起菜单", show=False)]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(markup=False, compact=True, **kwargs)
        self._commands: dict[str, str] = {}

    def on_mount(self) -> None:
        self.rebuild()
        # ``content_size`` is 0 until the first layout, so build again once the
        # box has a real width - otherwise the columns are padded for the
        # fallback width instead of the one that is actually on screen.
        self.call_after_refresh(self.rebuild)

    def rebuild(self, width: int = 0) -> None:
        """(Re)build every row for a ``width``-cell box (default: the real one)."""

        width = width or self.content_size.width or MENU_CONTENT
        keep = self.highlighted_option.id if self.highlighted_option else None
        options: list[Option] = []
        self._commands.clear()
        for section, entries in MENU:
            options.append(
                Option(_section_row(section, width), id=f"sec:{section}", disabled=True)
            )
            for index, entry in enumerate(entries):
                label, command = entry[0], entry[1]
                hint = entry[2] if len(entry) > 2 else command
                key = f"{section}:{index}"
                self._commands[key] = command
                options.append(Option(clip(pad(label, MENU_LABEL) + hint, width), id=key))
        self.set_options(options)
        if keep and keep in {option.id for option in options}:
            self.highlighted = self.get_option_index(keep)
        else:
            self.action_first()

    def command_for(self, option_id: str | None) -> str | None:
        """The slash command bound to a row, or ``None`` for section headers."""

        return self._commands.get(option_id or "")

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        command = self.command_for(event.option_id)
        if command:
            self.post_message(MenuCommand(command))

    def action_dismiss_menu(self) -> None:
        self.post_message(MenuDismiss())

