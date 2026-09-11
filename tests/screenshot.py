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
    ToolCallView,
    ToolResultView,
    UserMessage,
)

OUT = Path(__file__).resolve().parents[1] / "docs" / "screenshot.svg"

QUERY = "搜一下今天 AI 领域有什么值得关注的动态"

RESULTS = """\
搜索「今天 AI 领域有什么值得关注的动态」（duckduckgo，4 条）：

1. 国产大模型集体更新长上下文能力
   https://example.com/llm-long-context
   多家厂商同周发布 256K 以上上下文版本，价格战继续。
2. 具身智能融资回暖，两家公司完成新一轮
   https://example.com/embodied-funding
   本轮资金主要用于数据采集工厂与仿真环境建设。
3. AI 编程工具横向评测：补全准确率与仓库级重构
   https://example.com/coding-tools-review
   评测覆盖 6 款工具，仓库级重构仍是分水岭。
4. 端侧推理框架发布 1.0
   https://example.com/edge-inference
   支持 NPU 量化，手机端 7B 模型延迟下降约四成。"""

ANSWER = """\
今天 AI 领域值得关注的四条：

| 方向 | 动态 |
|------|------|
| 大模型 | 国产厂商集体更新长上下文，延续价格战 |
| 具身智能 | 融资回暖，钱主要投在数据采集与仿真 |
| 开发工具 | 编程助手评测更新，仓库级重构仍是分水岭 |
| 端侧推理 | 推理框架 1.0 支持 NPU 量化，7B 模型延迟降约 40% |

**跟你的关系**：第 3 条那篇评测直接对标你在做的工具链对比；\
第 4 条如果落地，本地跑 Ollama 的体验会明显变好。要我把这篇评测的正文抓下来细看吗？"""


async def main() -> None:
    app = JarvisApp(load_config())
    async with app.run_test(size=(132, 40)) as pilot:
        await pilot.pause()

        await app._append(UserMessage(QUERY))
        await app._append(ToolCallView("web_search", 'query="今天 AI 领域有什么值得关注的动态", max_results=4'))
        await app._append(ToolResultView("web_search", RESULTS, True))
        bubble = AssistantMessage()
        await app._append(bubble)
        await bubble.append_text(ANSWER)

        await app._append(Notice("▶ 模型  qwen  ·  deepseek 请求失败（429 额度用尽），已自动切换", "warn"))
        await app._append(Notice("· 本次调用 1 个工具，用时 4.1s", "info"))

        await pilot.pause()
        OUT.parent.mkdir(parents=True, exist_ok=True)
        svg = app.export_screenshot()
        OUT.write_text(svg, encoding="utf-8")

    print(f"saved {OUT} ({len(svg)} chars)")


if __name__ == "__main__":
    asyncio.run(main())
