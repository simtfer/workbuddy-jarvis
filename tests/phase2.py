"""Phase 2 checks: planner, facts, scheduler, hotkey - all without network calls.

Run with:  uv run python -u tests/phase2.py
"""

from __future__ import annotations

import asyncio
import faulthandler
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from jarvis.config import Config, DaemonConfig, MemoryConfig, ModelConfig, SecurityConfig
from jarvis.core.agent import Agent
from jarvis.core.planner import extract_json, parse_plan, step_instruction
from jarvis.core.registry import Tool, ToolRegistry
from jarvis.core.scheduler import (
    ScheduleError,
    ScheduledTask,
    Scheduler,
    TaskStore,
    parse_datetime,
    parse_task_command,
)
from jarvis.llm.client import StreamResult, ToolCall
from jarvis.memory.store import HistoryStore

faulthandler.dump_traceback_later(60, exit=True)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILURES.append(label)


# --------------------------------------------------------------------- fake LLM
class FakeClient:
    """Returns canned replies; records what the agent asked for."""

    def __init__(self, replies: list[object]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.tools_supported = True

    async def stream(self, messages, tools=None, on_delta=None, on_reasoning=None):
        self.calls.append({"messages": messages, "tools": tools})
        reply = self.replies.pop(0) if self.replies else ""
        if isinstance(reply, StreamResult):
            if on_reasoning and reply.reasoning:
                for index in range(0, len(reply.reasoning), 10):
                    on_reasoning(reply.reasoning[index : index + 10])
            if on_delta and reply.content:
                on_delta(reply.content)
            return reply
        text = str(reply)
        if on_delta and text:
            for index in range(0, len(text), 12):
                on_delta(text[index : index + 12])
        return StreamResult(content=text)

    async def aclose(self) -> None:
        pass


def make_config() -> Config:
    return Config(
        default_model="fake",
        models={
            "fake": ModelConfig(
                name="fake", base_url="http://127.0.0.1:1/v1", model="fake-model", api_key="x"
            )
        },
        security=SecurityConfig(max_tool_rounds=3),
        memory=MemoryConfig(auto_learn=True, learn_every=1, max_facts_in_prompt=5),
        daemon=DaemonConfig(),
    )


def make_agent(replies: list[object]) -> tuple[Agent, FakeClient]:
    """An Agent wired to a canned client (no network), plus that client."""

    client = FakeClient(replies)
    agent = Agent(
        config=make_config(),
        registry=make_registry(),
        client_factory=lambda _model: client,
    )
    return agent, client


def make_registry() -> ToolRegistry:
    registry = ToolRegistry(SecurityConfig())
    registry.register(
        Tool(
            name="echo_test",
            description="echo",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
            func=lambda text: f"echo:{text}",
        )
    )
    registry.register(
        Tool(
            name="danger_test",
            description="needs confirmation",
            parameters={"type": "object", "properties": {}},
            func=lambda: "boom",
            dangerous=True,
        )
    )
    return registry


# ---------------------------------------------------------------- planner tests
def test_planner() -> None:
    fenced = '```json\n{"goal": "g", "steps": [{"title": "A", "detail": "d1"}, {"title": "B"}]}\n```'
    steps = parse_plan(fenced)
    check("parse_plan 解析围栏 JSON", [s.title for s in steps] == ["A", "B"])
    check("parse_plan 保留 detail", steps[0].detail == "d1")

    loose = "好的，计划如下：\n1. 检查磁盘空间\n2. 清理临时文件\n3. 汇报结果"
    check("parse_plan 回退到编号列表", [s.title for s in parse_plan(loose)] == ["检查磁盘空间", "清理临时文件", "汇报结果"])

    check("extract_json 处理前后噪声", extract_json('前言 {"a": 1} 后语') == {"a": 1})
    check("extract_json 空输入返回 None", extract_json("") is None)
    check("step_instruction 带序号", "1/3" in step_instruction(0, 3, steps[0]))


# ------------------------------------------------------------------ facts tests
def test_facts(tmp: Path) -> HistoryStore:
    store = HistoryStore(tmp / "facts.db")
    first_id, created = store.add_fact("用户用 PowerShell 而不是 cmd")
    check("新增事实", created and first_id > 0)
    again_id, created_again = store.add_fact("用户用 PowerShell 而不是 cmd")
    check("重复事实被合并", (not created_again) and again_id == first_id)
    store.add_fact("项目路径在 D:\\code\\workbuddy-projects")
    check("事实计数", store.fact_count() == 2)
    check("按关键词搜索", len(store.search_facts("PowerShell")) == 1)
    check("fact 列表结构", store.facts()[0][1].startswith("项目路径"))
    check("删除事实", store.delete_fact(first_id) and store.fact_count() == 1)
    check("删除不存在的事实返回 False", store.delete_fact(9999) is False)
    store.add_fact("  多余   空白   会被压缩  ")
    check("空白压缩", any("多余 空白 会被压缩" == text for _id, text in store.facts()))
    return store


# -------------------------------------------------------------- scheduler tests
def test_scheduler(tmp: Path) -> None:
    conn = sqlite3.connect(tmp / "tasks.db", check_same_thread=False)
    task_store = TaskStore(conn)
    now = datetime(2026, 9, 11, 12, 0)

    def add(kind: str, spec: str, prompt: str) -> ScheduledTask:
        return task_store.add(
            ScheduledTask(id=None, kind=kind, spec=spec, prompt=prompt), now
        )

    interval = add("interval", "30", "每半小时")
    check(
        "interval 首次 next_run",
        parse_datetime(interval.next_run) == datetime(2026, 9, 11, 12, 30),
        interval.next_run,
    )

    # A run that fires late keeps the original cadence...
    task_store.mark_run(interval, datetime(2026, 9, 11, 12, 40))
    check(
        "interval 迟到执行后保持节奏",
        parse_datetime(task_store.get(interval.id).next_run) == datetime(2026, 9, 11, 13, 0),
        task_store.get(interval.id).next_run,
    )
    # ... but a long gap must not storm through every missed slot.
    task_store.mark_run(interval, datetime(2026, 9, 11, 14, 5))
    check(
        "interval 落后太多则从当下顺延",
        parse_datetime(task_store.get(interval.id).next_run) == datetime(2026, 9, 11, 14, 35),
        task_store.get(interval.id).next_run,
    )

    daily = add("daily", "09:00", "每天早九")
    check(
        "daily 当天已过则顺延",
        parse_datetime(daily.next_run) == datetime(2026, 9, 12, 9, 0),
        daily.next_run,
    )

    daily_later = add("daily", "18:30", "每天晚六半")
    check(
        "daily 当天未到用当天",
        parse_datetime(daily_later.next_run) == datetime(2026, 9, 11, 18, 30),
        daily_later.next_run,
    )

    once = add("once", "2026-09-11 13:00", "一次")
    check(
        "once 有下次时间",
        parse_datetime(once.next_run) == datetime(2026, 9, 11, 13, 0),
        once.next_run,
    )

    due_ids = lambda moment: {t.id for t in task_store.due(moment)}  # noqa: E731

    check("到期任务筛选：当前无到期", due_ids(now) == set())
    check("到期任务筛选：13:05 命中 once", due_ids(datetime(2026, 9, 11, 13, 5)) == {once.id})
    check(
        "到期任务筛选：14:40 命中 interval 与 once",
        due_ids(datetime(2026, 9, 11, 14, 40)) == {interval.id, once.id},
    )
    check("到期任务筛选：18:30 命中 daily_later", daily_later.id in due_ids(datetime(2026, 9, 11, 18, 30)))

    fired: list[int] = []

    async def runner(task: ScheduledTask) -> None:
        fired.append(task.id)  # type: ignore[arg-type]

    scheduler = Scheduler(task_store, runner=runner)
    ran = asyncio.run(scheduler.run_due_once(datetime(2026, 9, 11, 19, 0)))
    check(
        "run_due_once 全部跑完并记录",
        sorted(fired) == sorted(t.id for t in ran) and len(ran) == 3,
        f"ran={[t.id for t in ran]}",
    )

    after = task_store.get(once.id)
    check(
        "跑完的 once 自动停用",
        after.enabled is False and parse_datetime(after.last_run) == datetime(2026, 9, 11, 19, 0),
        f"enabled={after.enabled} last_run={after.last_run}",
    )

    task_store.set_enabled(interval.id, False)
    check(
        "停用任务不再出现在 due 里",
        interval.id not in due_ids(datetime(2027, 1, 1)) and daily_later.id in due_ids(datetime(2027, 1, 1)),
    )
    conn.close()


def test_task_parsing() -> None:
    task = parse_task_command("检查磁盘空间 @every 30m")
    check("解析 @every", (task.kind, task.spec, task.prompt) == ("interval", "30", "检查磁盘空间"))

    task = parse_task_command("汇总错误日志 @daily 9:5")
    check("解析 @daily 并补零", (task.kind, task.spec) == ("daily", "09:05"))

    task = parse_task_command("整理下载目录 @once 2026-09-12 08:00 --danger")
    check("解析 @once + --danger", (task.kind, task.spec, task.allow_dangerous) == ("once", "2026-09-12 08:00", True))

    task = parse_task_command("备份笔记 @every 2h")
    check("小时换算成分钟", task.spec == "120")

    for bad in ("没有调度表达式的任务", "任务 @every abc", "任务 @daily 25:00", "任务 @once 明天"):
        try:
            parse_task_command(bad)
        except ScheduleError:
            continue
        check(f"非法调度应报错: {bad}", False)
    check("非法调度全部被拦住", True)


# ------------------------------------------------------------------ agent tests
def test_agent_plan(tmp: Path) -> None:
    agent, client = make_agent(
        [
            '```json\n{"goal":"体检","steps":[{"title":"看磁盘","detail":"用 sys_report"},{"title":"汇报"}]}\n```',
            "磁盘还剩很多。",
            "已汇报完毕。",
        ]
    )

    async def run() -> None:
        events = [event async for event in agent.plan_task("做一次体检")]
        check("plan_task 产出 plan 事件", events[-1]["type"] == "plan" and len(events[-1]["steps"]) == 2)

        exec_events = [event async for event in agent.execute_plan()]
        kinds = [event["type"] for event in exec_events]
        check("执行计划有 step_start/step_done", kinds.count("step_start") == 2 and kinds.count("step_done") == 2)
        check("执行计划有 plan_done", kinds[-1] == "plan_done")
        check("步骤状态全部 done", all(step.status == "done" for step in agent.plan))
        check("规划请求未带工具", client.calls[0]["tools"] is None)
        check("多轮对话复用同一个客户端", client is agent._client)

    asyncio.run(run())
    asyncio.run(agent.aclose())


def test_agent_tools() -> None:
    agent, _client = make_agent(
        [
            StreamResult(
                content="",
                tool_calls=[ToolCall(id="c1", name="echo_test", arguments='{"text": "hi"}')],
            ),
            "工具返回了 echo:hi。",
        ]
    )

    async def run() -> None:
        events = [event async for event in agent.run("测试工具")]
        kinds = [event["type"] for event in events]
        check("工具调用事件", "tool_start" in kinds and "tool_result" in kinds)
        result = next(event for event in events if event["type"] == "tool_result")
        check("工具真的执行了", result["output"] == "echo:hi")
        answer = "".join(event.get("text", "") for event in events)
        check("最终文本回答", "echo:hi" in answer, answer)

    asyncio.run(run())
    asyncio.run(agent.aclose())


def test_agent_thinking() -> None:
    """Reasoning-model thinking stream: thinking events flow, text stays clean."""
    agent, _client = make_agent(
        [StreamResult(content="答案是 42。", reasoning="用户在问终极问题，我需要……先算一下。")]
    )

    async def run() -> None:
        events = [event async for event in agent.run("终极问题")]
        kinds = [event["type"] for event in events]
        thinking = [e for e in events if e["type"] == "thinking"]
        check("thinking 事件存在", len(thinking) > 0, str(kinds))
        check(
            "thinking 事件带 delta 而非 text 键",
            all("delta" in e and "text" not in e for e in thinking),
        )
        check(
            "thinking 增量拼回完整思考",
            "".join(e["delta"] for e in thinking) == "用户在问终极问题，我需要……先算一下。",
        )
        check(
            "text 累积不被思考污染",
            "".join(e.get("text", "") for e in events) == "答案是 42。",
        )
        first_thinking = next(i for i, e in enumerate(events) if e["type"] == "thinking")
        first_text = next(i for i, e in enumerate(events) if e["type"] == "text")
        check("thinking 先于正文", first_thinking < first_text)
        check("思考不进上下文历史", all("reasoning" not in str(m) for m in agent.messages))

    asyncio.run(run())
    asyncio.run(agent.aclose())


def test_agent_confirmation() -> None:
    agent, _client = make_agent(
        [
            StreamResult(content="", tool_calls=[ToolCall(id="c1", name="danger_test", arguments="{}")]),
            "好的，我停下。",
        ]
    )
    asked: list[str] = []

    async def deny(request) -> bool:
        asked.append(request.summary)
        return False

    agent.confirm_handler = deny

    async def run() -> None:
        events = [event async for event in agent.run("执行危险操作")]
        kinds = [event["type"] for event in events]
        result = next(event for event in events if event["type"] == "tool_result")
        check("危险工具触发确认", "confirm" in kinds and asked == ["danger_test"], str(asked))
        check("拒绝后不执行", result["ok"] is False and "拒绝" in result["output"])

    asyncio.run(run())
    asyncio.run(agent.aclose())


def test_agent_facts_prompt(tmp: Path, store: HistoryStore) -> None:
    store.add_fact("用户喜欢简洁的中文回答")
    agent = Agent(
        config=make_config(),
        registry=make_registry(),
        facts_provider=lambda: [text for _id, text in store.facts(5)],
        client_factory=lambda _model: FakeClient(['["用户偏好 PowerShell", "项目在 D 盘", 123, ""]']),
    )
    system = agent.messages[0]["content"]
    check("事实注入系统提示", "用户喜欢简洁的中文回答" in system and "长期记忆" in system)

    store.add_fact("项目用 uv 管理依赖")
    agent.refresh_system()
    check("刷新后包含新事实", "项目用 uv 管理依赖" in agent.messages[0]["content"])

    learned = asyncio.run(agent.learn("用户: 我在 D 盘写代码"))
    check("learn 提取字符串事实", learned == ["用户偏好 PowerShell", "项目在 D 盘"])
    asyncio.run(agent.aclose())

    agent.client_factory = lambda _model: FakeClient(["完全不是 JSON"])
    agent.drop_client()
    check("learn 解析失败返回空", asyncio.run(agent.learn("随便聊聊")) == [])
    asyncio.run(agent.aclose())


# ---------------------------------------------------------------- hotkey tests
def test_hotkey() -> None:
    import ctypes
    import time

    from jarvis.daemon.hotkey import WM_HOTKEY, GlobalHotkey, HotkeyError, parse_hotkey

    check("解析 ctrl+alt+j", parse_hotkey("ctrl+alt+j") == (0x0002 | 0x0001, ord("J")))
    check("解析 win+shift+f2", parse_hotkey("win+shift+f2")[1] == 0x71)
    for bad in ("j", "ctrl+", "ctrl+alt+不存在的键"):
        try:
            parse_hotkey(bad)
        except HotkeyError:
            continue
        check(f"非法热键应报错: {bad}", False)
    check("非法热键全部被拦住", True)

    fired: list[str] = []
    listener = GlobalHotkey("ctrl+alt+j", lambda: fired.append("hit")).start()
    if listener.error:
        print(f"[warn] 热键注册失败（不影响功能）：{listener.error}")
    else:
        check("全局热键注册成功", listener.registered)
        # Deliver the same message the OS sends when the key combo is pressed.
        ctypes.windll.user32.PostThreadMessageW(listener._thread_id, WM_HOTKEY, listener.hotkey_id, 0)
        time.sleep(0.6)
        check("收到 WM_HOTKEY 后回调触发", fired == ["hit"], str(fired))
    listener.stop()
    check("热键注销", listener.registered is False)


def main() -> int:
    # ignore_cleanup_errors: sqlite/WAL handles on Windows can linger a moment.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
        store = None
        try:
            test_planner()
            store = test_facts(tmp)
            test_scheduler(tmp)
            test_task_parsing()
            test_agent_plan(tmp)
            test_agent_tools()
            test_agent_thinking()
            test_agent_confirmation()
            test_agent_facts_prompt(tmp, store)
            test_hotkey()
        finally:
            if store is not None:
                store.close()

    faulthandler.cancel_dump_traceback_later()
    if FAILURES:
        print(f"\nPHASE2 TEST FAILED: {len(FAILURES)} 项未通过 -> {FAILURES}")
        return 1
    print("\nPHASE2 TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
