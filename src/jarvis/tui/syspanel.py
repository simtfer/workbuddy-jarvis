"""The right-hand system sidebar: sampling and rendering.

Extracted from ``app.py`` so the App no longer carries the psutil sampling
schedules. The panel keeps two clocks - cheap metrics every 2s, the process
table every 10s - and both stand down while the sidebar is collapsed: nobody
can see the numbers, so sampling them is pure waste.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from textual.widgets import Static

from ..textwidth import clip, pad
from ..tools import sysinfo

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .app import JarvisApp


class SystemPanel:
    """Owns the ``#syspanel`` Static: what it shows and when it refreshes.

    The App wires the timers (``set_interval``) and calls :meth:`tick_metrics`
    / :meth:`tick_processes`; the panel decides whether a sample is worth
    taking and renders the result into the widget.
    """

    # Sidebar label column, in cells. Wide enough for the longest label (a
    # three-glyph CJK word = 6 cells) plus a space, so every value in a section
    # starts on the same column instead of drifting with the label's width.
    LABEL = 7

    def __init__(self, app: JarvisApp) -> None:
        self.app = app
        self.data: dict[str, str] | None = None
        self.top_text = "(正在采集…)"
        self._metrics_busy = False
        self._top_busy = False

    # ------------------------------------------------------------------ state
    def side_hidden(self) -> bool:
        try:
            return self.app.query_one("#side").has_class("hidden")
        except Exception:  # noqa: BLE001 - during teardown the widget is gone
            return True

    def set_text(self, text: str) -> None:
        try:
            self.app.query_one("#syspanel", Static).update(text)
        except Exception:  # noqa: BLE001 - cosmetic
            pass

    def toggle(self) -> bool:
        """Flip the sidebar; returns True when it just became visible."""

        side = self.app.query_one("#side")
        side.toggle_class("hidden")
        if side.has_class("hidden"):
            return False
        # Sample on open: while the panel is collapsed its timers stay quiet, so
        # the numbers would otherwise be missing (or stale) on the first look.
        self.set_text("正在采集系统状态…")
        self.app.run_worker(self.sample_now(), group="panel-open", exclusive=True)
        return True

    # ----------------------------------------------------------------- clocks
    async def sample_now(self) -> None:
        """Fetch both halves of the panel, cheap metrics first.

        One worker, not two racing: the process walk holds the shared psutil
        lock for seconds, so starting it alongside the metrics can delay
        exactly the numbers the user just opened the panel to see.
        """

        if self._metrics_busy or self._top_busy:
            return
        self._top_busy = True
        try:
            await self._fetch_metrics()
            if not self.side_hidden():
                await self._fetch_top()
        finally:
            self._top_busy = False

    def tick_metrics(self) -> None:
        """Timer tick (2s): sample the cheap system metrics off the UI thread.

        Walking every process with psutil costs seconds on a busy machine, so
        the process table has its own slower clock (:meth:`tick_processes`).
        Both run in worker threads: a slow sample can never freeze the window.
        """

        if self.side_hidden() or self._metrics_busy:
            return
        self._metrics_busy = True
        self.app.run_worker(self._fetch_metrics(), group="panel", exclusive=True)

    def tick_processes(self) -> None:
        """Timer tick (10s): refresh the (expensive) TOP 进程 table.

        Yields to the cheap panel metrics: the process walk holds the shared
        psutil lock for seconds, and queueing it in front of the 2s metrics
        would stall the panel the user is looking at.
        """

        if self.side_hidden() or self._top_busy or self._metrics_busy:
            return
        self._top_busy = True
        self.app.run_worker(self._fetch_top(), group="top", exclusive=True)

    async def _fetch_metrics(self) -> None:
        try:
            data = await asyncio.to_thread(sysinfo.snapshot, 0)
        except Exception:  # noqa: BLE001 - panel is cosmetic, never crash the UI
            return
        finally:
            self._metrics_busy = False
        self.data = data
        self.render()

    async def _fetch_top(self) -> None:
        try:
            text = await asyncio.to_thread(sysinfo.top_processes, 5)
        except Exception:  # noqa: BLE001 - panel is cosmetic, never crash the UI
            return
        finally:
            self._top_busy = False
        self.top_text = text
        self.render()

    # --------------------------------------------------------------- rendering
    def render(self) -> None:
        try:
            panel = self.app.query_one("#syspanel", Static)
        except Exception:  # noqa: BLE001 - widget may already be gone
            return
        # Measure the real box rather than trusting the CSS constant: before the
        # first layout ``content_size`` is 0, so fall back to the fixed width.
        width = panel.content_size.width
        if not width or width < 20:
            width = sysinfo.PANEL_WIDTH
        panel.update("\n".join(self.lines(width)))

    def lines(self, width: int) -> list[str]:
        """Build the sidebar as fixed rows, every one clipped to ``width``.

        Nothing here may exceed ``width``: ``#side`` does not scroll, so an
        overlong line wraps and the column layout collapses.
        """

        app = self.app
        data: dict[str, Any] = self.data or {}
        model = app.agent.model
        tasks = app.task_store.list()
        next_task = min(
            (task for task in tasks if task.enabled and task.next_run),
            key=lambda task: task.next_run,
            default=None,
        )

        def row(label: str, value: str) -> str:
            return clip(pad(label, self.LABEL) + value, width)

        def fit(text: str) -> list[str]:
            return [clip(line, width) for line in str(text).splitlines()] or [""]

        lines = ["SYSTEM", clip(str(data.get("host", "")), width), ""]
        lines.append(row("CPU", str(data.get("cpu", ""))))
        lines += [
            clip(line, width)
            for line in sysinfo.core_rows(data.get("cores", []), indent=self.LABEL)
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
        lines += fit(self.top_text)
        lines += [
            "",
            "SESSION",
            row("模型", f"{model.display} · {model.model}"),
            row("来源", f"{model.provider or '自定义'} · {model.base_url.split('//')[-1]}"),
            row("工具", f"{len(app.registry.tools)} 个"),
            row(
                "搜索",
                app.config.search.provider + ("" if app.config.search.enabled else "（关闭）"),
            ),
            row("上下文", f"{len(app.agent.history())} 条消息"),
            row("记忆", f"{app.store.fact_count()} 条事实"),
            row("计划", f"{len(app.agent.plan)} 步" + (" (执行中)" if app._busy else "")),
            row(
                "任务",
                f"{len([task for task in tasks if task.enabled])} 个启用"
                + (f"，下次 {next_task.next_run[5:16]}" if next_task else ""),
            ),
            row("会话", app.session_id),
        ]
        return lines
