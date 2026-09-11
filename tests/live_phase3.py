"""Live Phase 3 check: real model doing a real web search, plus real failover.

Four bounded calls, no fake clients anywhere:
  1. the model answers normally,
  2. the model decides to call ``web_search`` and uses the live results,
  3. ``fetch_url`` pulls a real page down,
  4. a model pointed at a dead port fails over to a working one over real HTTP.

Needs a valid API key in config.toml or the environment.
Run with:  uv run python -u tests/live_phase3.py
"""

from __future__ import annotations

import asyncio
import faulthandler
import re
import sys
from dataclasses import replace

from jarvis.config import ModelConfig, load_config
from jarvis.core.agent import Agent
from jarvis.tools import build_registry
from jarvis.tools import web

faulthandler.dump_traceback_later(420, exit=True)

SEARCH_QUERY = "今天有什么科技新闻"
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  ({detail})" if detail else ""), flush=True)
    if not condition:
        FAILURES.append(label)


def announce(event: dict) -> None:
    kind = event["type"]
    if kind == "text":
        print(event["text"], end="", flush=True)
    elif kind == "notice":
        print(f"\n      {event['text']}", flush=True)
    elif kind == "tool_start":
        print(f"\n      [tool] {event['name']} {event['arguments']}", flush=True)
    elif kind == "tool_result":
        print(f"      [result] {event['output'][:140]!r}", flush=True)
    elif kind == "error":
        print(f"\n      ERROR: {event['message']}", flush=True)


async def main() -> int:
    config = load_config()
    if not config.model().resolve_api_key():
        print(f"跳过：{config.default_model} 没有 API Key（配好再跑）。")
        return 0

    registry = build_registry(config.security, str(config.workdir), config.search)
    agent = Agent(config=config, registry=registry)
    agent.confirm_handler = lambda request: _approve(request)
    print(f"model : {agent.model.display} / {agent.model.model}", flush=True)
    print(f"search: {web.describe(config.search)}\n", flush=True)

    # ------------------------------------------------- 1. plain conversation
    print("--- 1. 普通对话 ---", flush=True)
    events = [event async for event in agent.run("用一句话说明你是谁")]
    answer = "".join(event.get("text", "") for event in events)
    errors = [e["message"] for e in events if e["type"] == "error"]
    check("真模型能回答", len(answer.strip()) > 0 and not errors, answer.strip()[:80])

    # ------------------------------------------- 2. model-driven web search
    agent.reset()
    print(f"\n--- 2. 模型自主联网搜索：{SEARCH_QUERY} ---", flush=True)
    events = [event async for event in agent.run(SEARCH_QUERY)]
    for event in events:
        announce(event)
    used = [e["name"] for e in events if e["type"] == "tool_start"]
    result = next((e["output"] for e in events if e["type"] == "tool_result"), "")
    final = "".join(e.get("text", "") for e in events if e["type"] == "text")
    numbered = [ln for ln in result.splitlines() if re.match(r"^\d+\. ", ln)]
    check("模型调用了 web_search", "web_search" in used, str(used))
    # Careful: an error message also contains the URL, so do not just test for "http".
    check(
        "搜索返回了真实结果（非报错）",
        not result.startswith("[错误]") and bool(numbered) and "http" in result,
        f"{len(numbered)} 条 · {result.splitlines()[0][:70] if result else '空'}",
    )
    check("模型基于搜索结果作答", len(final.strip()) > 30, final.strip()[:90])

    # ------------------------------------------------------ 3. fetch_url
    print("\n--- 3. 抓取真实网页 ---", flush=True)
    page = await registry.call("fetch_url", {"url": "https://example.com", "max_chars": 300})
    check("fetch_url 抓到正文", "Example Domain" in page, page[:90].replace("\n", " "))

    # ------------------------------------------------ 4. real failover
    print("\n--- 4. 真实故障切换（死端口 → 可用模型）---", flush=True)
    dead = ModelConfig(
        name="dead",
        label="死端口",
        base_url="http://127.0.0.1:9/v1",  # nothing listens here
        model="ghost",
        api_key="none",
        timeout=5,
    )
    cfg = replace(config, models={"dead": dead, **config.models}, default_model="dead")
    cfg.fallback_models = [config.default_model]
    survivor = Agent(
        config=cfg,
        registry=build_registry(cfg.security, str(cfg.workdir), cfg.search),
        model_name="dead",
    )
    events = [event async for event in survivor.run("只回答：好")]
    notices = [e["text"] for e in events if e["type"] == "notice"]
    for event in events:
        if event["type"] in {"notice", "error"}:
            print(f"      {event.get('text') or event.get('message')}", flush=True)
    text = "".join(e.get("text", "") for e in events if e["type"] == "text")
    check("死端口真的失败了", any("请求失败" in n or "不可用" in n for n in notices), str(notices[:1]))
    check("自动切到可用模型", survivor.model_key == config.default_model, survivor.model_key)
    check("备用模型给出了回答", bool(text.strip()), text.strip()[:40])

    faulthandler.cancel_dump_traceback_later()
    if FAILURES:
        print(f"\nLIVE PHASE3 FAILED: {len(FAILURES)} 项未通过 -> {FAILURES}")
        return 1
    print("\nLIVE PHASE3 PASSED")
    return 0


async def _approve(request) -> bool:
    print(f"\n      [confirm] {request.summary!r} -> 自动同意", flush=True)
    return True


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
