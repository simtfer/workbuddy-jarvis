"""Live Phase 2 check: real model for plan mode, scheduled runs and memory.

Kept deliberately small: 1 planning call, 1 plan step, 1 memory pass, 1 task run.
Needs a valid API key in config.toml or the environment.
Run with:  uv run python -u tests/live_phase2.py
"""

from __future__ import annotations

import asyncio
import faulthandler
import sys
import tempfile
from pathlib import Path

from jarvis.config import load_config
from jarvis.core.agent import Agent, ConfirmRequest
from jarvis.core.scheduler import ScheduledTask, TaskStore
from jarvis.daemon.service import run_task
from jarvis.memory.store import HistoryStore
from jarvis.tools import build_registry

faulthandler.dump_traceback_later(300, exit=True)

PLAN_TASK = "报告这台机器的磁盘占用情况"
STEP_LIMIT = 1  # execute only the first step to keep the live run short


async def auto_approve(request: ConfirmRequest) -> bool:
    print(f"      [confirm] {request.summary!r} -> 自动同意", flush=True)
    return True


async def main() -> int:
    config = load_config()
    registry = build_registry(config.security, str(config.workdir))
    agent = Agent(
        config=config,
        registry=registry,
        confirm_handler=auto_approve,
        on_message=lambda message: None,
    )
    print(f"model : {agent.model.display} / {agent.model.model}\n", flush=True)

    # ------------------------------------------------------------- plan mode
    print(f"--- plan mode: {PLAN_TASK} ---", flush=True)
    async for event in agent.plan_task(PLAN_TASK):
        if event["type"] == "notice":
            print(f"      {event['text']}", flush=True)
        elif event["type"] == "error":
            print(f"ERROR: {event['message']}", flush=True)
            return 1

    check("计划步骤 >= 2", len(agent.plan) >= 2, f"{len(agent.plan)} 步")
    for index, step in enumerate(agent.plan, start=1):
        print(f"  {index}. {step.title} — {step.detail[:60]}", flush=True)

    print(f"\n--- execute first {STEP_LIMIT} step(s) ---", flush=True)
    agent.plan = agent.plan[:STEP_LIMIT]
    async for event in agent.execute_plan():
        kind = event["type"]
        if kind == "step_start":
            print(f"\n  ▶ {event['index'] + 1}/{event['total']} {event['title']}", flush=True)
        elif kind == "text":
            print(event["text"], end="", flush=True)
        elif kind == "tool_start":
            print(f"\n      [tool] {event['name']} {event['arguments']}", flush=True)
        elif kind == "tool_result":
            print(f"      [result] {event['output'][:110]!r}", flush=True)
        elif kind == "plan_done":
            print(f"\n  plan done: {event['done']}/{event['total']}", flush=True)
        elif kind == "error":
            print(f"\n  ERROR: {event['message']}", flush=True)

    print(f"\nstatus: {[step.status for step in agent.plan]}", flush=True)
    check("步骤执行成功", all(step.status == "done" for step in agent.plan))

    # --------------------------------------------------------- memory distill
    print("\n--- memory distill ---", flush=True)
    async for _event in agent.run("以后报告磁盘时，请按 C 盘和 D 盘分两行简要说，不要长篇大论。"):
        pass
    facts = await agent.learn(agent.transcript())
    print(f"learned: {facts or '(无)'}", flush=True)
    check("记忆提炼返回列表", isinstance(facts, list))
    await agent.aclose()

    # ------------------------------------------------------- scheduled task run
    print("\n--- scheduled task (daemon path) ---", flush=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = HistoryStore(Path(tmp) / "daemon.db")
        task = TaskStore(store.conn).add(
            ScheduledTask(id=None, kind="interval", spec="60", prompt="用一句话报告当前内存占用百分比")
        )
        print(f"task  : #{task.id} {task.schedule_text} next={task.next_run}", flush=True)
        answer = await run_task(config, store, task)
        saved = store.recent_messages(f"task-{task.id}", 5)
        print(f"answer: {answer[:200]}", flush=True)
        print(f"saved : {len(saved)} 条", flush=True)
        check("定时任务有产出", bool(answer) and answer != "(无输出)")
        check("定时任务结果落库", bool(saved))
        store.close()

    faulthandler.cancel_dump_traceback_later()
    if FAILURES:
        print(f"\nLIVE PHASE2 TEST FAILED: {FAILURES}", flush=True)
        return 1
    print("\nLIVE PHASE2 TEST PASSED", flush=True)
    return 0


FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  ({detail})" if detail else ""), flush=True)
    if not condition:
        FAILURES.append(label)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
