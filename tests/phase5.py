"""Phase 5: parallel sub-agents (``delegate_subagents`` tool).

Covers the new SubAgentManager surface and the Agent.run interception that
turns ``delegate_subagents`` tool calls into a fan-out of independent sub-agents
with a partial snapshot at ``default_timeout`` and a final summary once they
all settle.

Tests use a fake ``LLMClient`` so they run without a real model. The fake
honours a ``replies`` queue and a per-call ``delay`` so we can simulate both
fast and slow sub-agents.
"""

from __future__ import annotations

import asyncio
import faulthandler
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\code\workbuddy-projects\project1\jarvis")
sys.path.insert(0, str(ROOT / "src"))

from jarvis.config import SecurityConfig, SubAgentConfig, build_default_config
from jarvis.core.agent import Agent
from jarvis.core.registry import ToolRegistry
from jarvis.core.subagent import SUBAGENT_TOOL_NAME, SubAgentManager
from jarvis.llm.client import StreamResult, ToolCall


# ---------------------------------------------------------------- helpers
def check(label: str, ok: bool, hint: str = "") -> bool:
    mark = "OK " if ok else "FAIL"
    line = f"[{mark}] {label}"
    if hint:
        line += f"  ({hint})"
    print(line)
    return ok


class FakeClient:
    """A drop-in LLMClient: replies come from a queue; per-call delay is configurable."""

    def __init__(self, replies: list, delays: list[float] | None = None):
        self.replies = list(replies)
        self.delays = list(delays or [0.01] * len(replies))
        self.calls = 0

    async def aclose(self) -> None:
        return None

    async def stream(self, messages, tools=None, on_delta=None):
        idx = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        await asyncio.sleep(self.delays[idx])
        reply = self.replies[idx]
        if on_delta is not None and isinstance(reply, str):
            on_delta(reply)
        if isinstance(reply, str):
            return StreamResult(content=reply, tool_calls=None)
        if isinstance(reply, ToolCall):
            return StreamResult(content="", tool_calls=[reply])
        if isinstance(reply, list) and reply and isinstance(reply[0], ToolCall):
            return StreamResult(content="", tool_calls=reply)
        raise RuntimeError(f"unsupported reply: {reply!r}")


def make_agent(
    cfg,
    parent_replies: list,
    child_replies: list,
    child_delays: list[float] | None = None,
) -> tuple[Agent, FakeClient, FakeClient]:
    """Build an Agent whose parent and children use the supplied fake clients."""

    parent_client = FakeClient(parent_replies)
    child_client = FakeClient(child_replies, child_delays)

    call_index = {"n": 0}
    real_build = Agent._build_client

    def fake_build(self, model_config):
        if call_index["n"] == 0:
            call_index["n"] += 1
            return parent_client
        return child_client

    Agent._build_client = fake_build  # type: ignore[assignment]
    registry = ToolRegistry(SecurityConfig())
    from jarvis.tools import register_subagent_tool
    register_subagent_tool(registry)
    agent = Agent(config=cfg, registry=registry, model_name="deepseek")
    return agent, parent_client, child_client


def restore_agent_build_client(real_build) -> None:
    Agent._build_client = real_build  # type: ignore[assignment]


# ---------------------------------------------------------------- tests
def test_validate_rejects_empty() -> None:
    cfg = build_default_config()
    cfg.subagents = SubAgentConfig(default_timeout=0.1, max_runtime=1.0)
    registry = ToolRegistry(SecurityConfig())
    parent = Agent(config=cfg, registry=registry, model_name="deepseek")
    manager = SubAgentManager(parent)

    assert check("空列表被拒", manager.validate([]) is not None)
    assert check("非列表被拒", manager.validate("not-a-list") is not None)
    assert check("空白字符串被拒", manager.validate(["a", "  "]) is not None)
    assert check("列表过长被拒",
                 manager.validate([f"p{i}" for i in range(cfg.subagents.max_per_call + 1)]) is not None)
    assert check("正常列表通过", manager.validate(["a", "b", "c"]) is None)


def test_validate_respects_max_per_call() -> None:
    cfg = build_default_config()
    cfg.subagents = SubAgentConfig(max_per_call=2)
    registry = ToolRegistry(SecurityConfig())
    parent = Agent(config=cfg, registry=registry, model_name="deepseek")
    manager = SubAgentManager(parent)

    assert check("2 个通过", manager.validate(["a", "b"]) is None)
    assert check("3 个被拒", manager.validate(["a", "b", "c"]) is not None)


async def _events_for(
    prompts: list[str],
    parent_replies,
    child_replies,
    child_delays=None,
    cfg=None,
) -> list[dict]:
    if cfg is None:
        cfg = build_default_config()
        cfg.subagents = SubAgentConfig(
            default_timeout=0.2, max_runtime=2.0, max_concurrent=4, max_per_call=8
        )
    real_build = Agent._build_client
    agent, _parent, _child = make_agent(cfg, parent_replies, child_replies, child_delays)
    try:
        events = []
        async for event in agent.run(prompts[0] if prompts else ""):
            events.append(event)
        return events
    finally:
        restore_agent_build_client(real_build)


def test_fast_subagents_no_timeout() -> None:
    async def run():
        cfg = build_default_config()
        cfg.subagents = SubAgentConfig(default_timeout=0.5, max_runtime=2.0)
        # Parent: call subagent with two fast prompts.
        tool_call = ToolCall(
            id="c1",
            name=SUBAGENT_TOOL_NAME,
            arguments='{"prompts": ["A", "B"]}',
        )
        return await _events_for(
            ["x"],
            parent_replies=[tool_call, "完成"],
            child_replies=["reply-A", "reply-B"],
            child_delays=[0.01, 0.01],
        )
    events = asyncio.run(run())
    types = [e["type"] for e in events]
    print("test_fast:", types)
    assert check("subagent_start", "subagent_start" in types)
    assert check("两个 subagent_done", types.count("subagent_done") == 2, str(types.count("subagent_done")))
    assert check("subagent_timeout 不应出现（都很快）", "subagent_timeout" not in types, str(types))
    assert check("subagent_final 出现", "subagent_final" in types)
    assert check("tool_result 出现", "tool_result" in types)
    assert check("最终 done", types[-1] == "done")
    # Summary content
    final_summary = next(
        e for e in events if e["type"] == "tool_result" and e.get("name") == SUBAGENT_TOOL_NAME
    )
    assert check("summary 含 2/2", "2/2" in final_summary["output"], final_summary["output"][:60])


def test_slow_subagent_triggers_timeout() -> None:
    async def run():
        tool_call = ToolCall(
            id="c1",
            name=SUBAGENT_TOOL_NAME,
            arguments='{"prompts": ["fast", "slow"]}',
        )
        return await _events_for(
            ["x"],
            parent_replies=[tool_call, "完成"],
            child_replies=["reply-fast", "reply-slow"],
            child_delays=[0.01, 0.6],   # slow > default_timeout (0.2)
        )
    events = asyncio.run(run())
    types = [e["type"] for e in events]
    print("test_timeout:", types)
    assert check("subagent_timeout 出现", "subagent_timeout" in types, str(types))
    # The slow subagent's done event must come *after* the timeout.
    timeout_idx = types.index("subagent_timeout")
    slow_done_idxs = [
        i for i, e in enumerate(events)
        if e.get("type") == "subagent_done" and e.get("index") == 1
    ]
    assert check("慢任务 done 在 timeout 之后", slow_done_idxs and slow_done_idxs[0] > timeout_idx,
                 str((timeout_idx, slow_done_idxs)))


def test_max_concurrent_signal() -> None:
    """6 prompts, max_concurrent=2 — peak in-flight must be <= 2."""

    async def run():
        cfg = build_default_config()
        cfg.subagents = SubAgentConfig(default_timeout=5.0, max_runtime=10.0, max_concurrent=2)
        registry = ToolRegistry(SecurityConfig())
        parent = Agent(config=cfg, registry=registry, model_name="deepseek")

        state = {"current": 0, "peak": 0}
        barrier = asyncio.Event()

        class CountingClient:
            async def aclose(self) -> None:
                return None
            async def stream(self, messages, tools=None, on_delta=None):
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
                await barrier.wait()
                state["current"] -= 1
                if on_delta is not None:
                    on_delta("done")
                return StreamResult(content="ok", tool_calls=None)

        real_build = Agent._build_client
        Agent._build_client = lambda self, model_config: CountingClient()  # type: ignore[assignment]
        try:
            async def drive():
                async for _ in SubAgentManager(parent).run([f"p{i}" for i in range(6)]):
                    pass
            task = asyncio.create_task(drive())
            await asyncio.sleep(0.05)
            barrier.set()
            await task
        finally:
            restore_agent_build_client(real_build)
        return state["peak"]

    peak = asyncio.run(run())
    print(f"test_max_concurrent: peak={peak}")
    assert check("peak 不超过 max_concurrent", peak <= 2, str(peak))


def test_subagents_have_isolated_messages() -> None:
    """Children must have their own message history; writing in one must not leak."""

    async def run():
        cfg = build_default_config()
        cfg.subagents = SubAgentConfig(default_timeout=0.5, max_runtime=2.0)
        tool_call = ToolCall(
            id="c1",
            name=SUBAGENT_TOOL_NAME,
            arguments='{"prompts": ["alpha", "beta"]}',
        )
        events = await _events_for(
            ["x"],
            parent_replies=[tool_call, "OK"],
            child_replies=["alpha-out", "beta-out"],
        )
        return events
    events = asyncio.run(run())

    # Reach into the manager via the final results — they carry the prompt
    # and status, which we can inspect.
    final = next(e for e in events if e["type"] == "subagent_final")
    assert check("两个子任务独立完成",
                 [r["status"] for r in final["results"]] == ["done", "done"], str(final["results"]))


def test_validation_error_returns_error_string() -> None:
    """Bad arguments to delegate_subagents become a [错误] tool result, no spawn."""

    async def run():
        cfg = build_default_config()
        cfg.subagents = SubAgentConfig(default_timeout=0.5, max_runtime=2.0, max_per_call=1)
        bad_call = ToolCall(
            id="c1",
            name=SUBAGENT_TOOL_NAME,
            arguments='{"prompts": ["a", "b"]}',   # exceeds max_per_call=1
        )
        return await _events_for(
            ["x"],
            parent_replies=[bad_call, "OK"],
            child_replies=[],   # should not be called
            cfg=cfg,
        )
    events = asyncio.run(run())
    types = [e["type"] for e in events]
    print("test_validation:", types)
    assert check("没有 subagent_start（被参数校验拦截）", "subagent_start" not in types, str(types))
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert check("tool_result 出现", bool(tool_results))
    last = tool_results[-1]
    assert check("错误信息以 [错误] 开头", last["output"].startswith("[错误]"), last["output"][:60])
    assert check("ok=false", last.get("ok") is False)


def test_subagent_summary_reaches_main_history() -> None:
    """After a delegate call, the parent Agent.messages has the summary as a tool msg."""

    async def run():
        cfg = build_default_config()
        cfg.subagents = SubAgentConfig(default_timeout=0.5, max_runtime=2.0)
        tool_call = ToolCall(
            id="c1",
            name=SUBAGENT_TOOL_NAME,
            arguments='{"prompts": ["task X"]}',
        )

        # Build manually so we can hold a reference to the agent.
        real_build = Agent._build_client
        from jarvis.tools import register_subagent_tool
        registry = ToolRegistry(SecurityConfig())
        register_subagent_tool(registry)
        parent_client = FakeClient([tool_call, "synthesis"])
        child_client = FakeClient(["answer-X"])

        def fake_build(self, model_config):
            if not getattr(self, "_saw_parent", False):
                self._saw_parent = True
                return parent_client
            return child_client

        Agent._build_client = fake_build  # type: ignore[assignment]
        try:
            agent = Agent(config=cfg, registry=registry, model_name="deepseek")
            async for _ in agent.run("派任务"):
                pass
            return agent
        finally:
            restore_agent_build_client(real_build)

    agent = asyncio.run(run())
    tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
    assert check("至少一条 tool 消息", len(tool_msgs) >= 1, str(len(tool_msgs)))
    summary = tool_msgs[-1]
    assert check("tool_call_id 已设置", "tool_call_id" in summary)
    assert check("summary 含 '并行子任务'", "并行子任务" in summary["content"], summary["content"][:80])


def main() -> int:
    faulthandler.dump_traceback_later(60, exit=True)
    tests = [
        test_validate_rejects_empty,
        test_validate_respects_max_per_call,
        test_fast_subagents_no_timeout,
        test_slow_subagent_triggers_timeout,
        test_max_concurrent_signal,
        test_subagents_have_isolated_messages,
        test_validation_error_returns_error_string,
        test_subagent_summary_reaches_main_history,
    ]
    passed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            print(f"[FAIL] {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"[FAIL] {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    faulthandler.cancel_dump_traceback_later()
    total = len(tests)
    print(f"\nPHASE5 {'PASSED' if passed == total else 'FAILED'}: {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())