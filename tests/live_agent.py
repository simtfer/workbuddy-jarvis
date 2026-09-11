"""Live end-to-end check: real model request + real tool execution.

Needs a valid API key in config.toml or the environment.
Run with:  uv run python -u tests/live_agent.py
"""

from __future__ import annotations

import asyncio
import sys

from jarvis.config import load_config
from jarvis.core.agent import Agent, ConfirmRequest
from jarvis.tools import build_registry

PROMPT = "用 run_shell 工具执行命令 echo jarvis-ok，然后用一句话告诉我输出是什么。不要执行其他命令。"


async def auto_approve(request: ConfirmRequest) -> bool:
    print(f"      [confirm] {request.summary!r} -> 自动同意")
    return True


async def main() -> int:
    config = load_config()
    registry = build_registry(config.security, str(config.workdir))
    agent = Agent(
        config=config,
        registry=registry,
        confirm_handler=auto_approve,
        on_message=lambda m: None,
    )
    print(f"model : {agent.model.display} / {agent.model.model}")
    print(f"prompt: {PROMPT}\n")

    text_parts: list[str] = []
    tools_used: list[str] = []
    async for event in agent.run(PROMPT):
        kind = event["type"]
        if kind == "text":
            text_parts.append(event["text"])
            print(event["text"], end="", flush=True)
        elif kind == "tool_start":
            tools_used.append(event["name"])
            print(f"\n      [tool] {event['name']} {event['arguments']}")
        elif kind == "tool_result":
            print(f"      [result] {event['output'][:200]!r}")
        elif kind == "error":
            print(f"\nERROR: {event['message']}")
            return 1
        elif kind == "done":
            pass

    answer = "".join(text_parts)
    await agent.aclose()
    print("\n")
    print(f"answer : {answer.strip()[:300]}")
    print(f"tools  : {tools_used}")
    assert tools_used, "模型没有调用任何工具"
    assert "jarvis-ok" in answer or "jarvis-ok" in str(answer), "回答里没有命令输出"
    print("LIVE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
