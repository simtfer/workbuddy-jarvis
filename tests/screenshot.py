"""Render a representative JARVIS session headlessly and export an SVG snapshot.

Run with:  uv run python -u tests/screenshot.py   ->  docs/screenshot.svg
No API key needed: the conversation is injected directly into the chat view.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from jarvis.config import load_config
from jarvis.tui.app import JarvisApp
from jarvis.tui.widgets import (
    AssistantMessage,
    Notice,
    PlanView,
    ToolCallView,
    ToolResultView,
    UserMessage,
)

OUT = Path(__file__).resolve().parents[1] / "docs" / "screenshot.svg"

PLAN = [
    {"title": "读取磁盘与分区信息", "detail": "sys_report + Get-Disk", "status": "done"},
    {"title": "对比 C/D 盘余量", "detail": "算出使用率与可用空间", "status": "running"},
    {"title": "给出结论与建议", "detail": "一句话总结", "status": "pending"},
]

ANSWER = """\
本机状态如下：

| 项目 | 数值 |
|------|------|
| CPU | 28.8% · 16 逻辑核 |
| 内存 | 58.1% · 18.4 GB / 31.7 GB |
| 磁盘 C: | 66% 已用，剩 65.4 GB |

**结论**：内存占用偏高，主要是 `MemCompression`（1.7 GB）和几个 Node 进程。\
要不要我看一下具体是哪些进程在吃内存？"""


async def main() -> None:
    app = JarvisApp(load_config())
    async with app.run_test(size=(132, 38)) as pilot:
        await pilot.pause()

        await app._append(UserMessage("/plan 报告这台机器的磁盘占用情况"))
        await app._append(Notice("正在为「报告这台机器的磁盘占用情况」制定计划…", "info"))
        await app._append(PlanView("报告这台机器的磁盘占用情况", PLAN))
        await app._append(Notice("共 3 步。执行：/do（逐步跑完）· 调整：/reset 后重新 /plan。", "info"))
        await app._append(ToolCallView("sys_report", "include_processes=8"))
        await app._append(
            ToolResultView(
                "sys_report",
                "主机: tang · Windows 11\nCPU: 28.8% · 16 逻辑核\n内存: 58.1% · 18.4 GB / 31.7 GB",
                True,
            )
        )
        bubble = AssistantMessage()
        await app._append(bubble)
        await bubble.append_text(ANSWER)
        await app._append(
            Notice("沉淀了 1 条长期记忆：\n+ 报告磁盘时按 C 盘和 D 盘分两行简要说明，不要长篇大论。", "info")
        )
        await app._append(Notice("· 本次调用 1 个工具，用时 3.4s", "info"))

        await pilot.pause()
        OUT.parent.mkdir(parents=True, exist_ok=True)
        svg = app.export_screenshot()
        OUT.write_text(svg, encoding="utf-8")

    print(f"saved {OUT} ({len(svg)} chars)")


if __name__ == "__main__":
    asyncio.run(main())
