"""Sub-agents: the main ``Agent`` farms independent tasks to a pool of children.

The point is to keep the chat moving while slow work is still cooking. ``run``
yields lifecycle events for the TUI and the final event carries the tool-result
string the calling ``Agent`` will record.

* ``subagent_start`` — right after the children are spawned, before any run.
* ``subagent_done`` — fired *per child* as it finishes (UI prints ✓ rows).
* ``subagent_timeout`` — fired *once* at ``default_timeout`` if anyone is still
  going; carries a snapshot of who is done vs still running, so the chat has
  something to show without waiting for the slowest task.
* ``subagent_final`` — fired *once* after every child has settled.
* ``subagent_summary`` — the final event; its ``text`` is the string the main
  agent will see as the tool result. Always reflects the complete picture.

Sub-agents are real ``Agent`` instances: independent message history, separate
LLM connection pool, fresh system prompt. They share the parent's
``Config`` / ``ToolRegistry`` / ``Store`` so writes (e.g. ``/remember``) end up
in the same DB. They do **not** inherit the parent's ``confirm_handler`` —
``Agent.run`` will see no handler and auto-deny any dangerous tool, which is the
right default for background workers.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator

from ..config import Config
from ..core.registry import ToolRegistry

if TYPE_CHECKING:  # pragma: no cover - type-only
    from .agent import Agent


SUBAGENT_TOOL_NAME = "delegate_subagents"


@dataclass
class SubAgent:
    """One in-flight child. Mutated in place as it runs."""

    index: int
    prompt: str
    status: str = "pending"          # pending | running | done | error | timeout
    output: str = ""                 # the assistant's final text (or error message)
    tool_count: int = 0
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    _emitted_done: bool = False      # one-shot flag for the ``subagent_done`` event

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.monotonic()
        return end - self.started_at

    def to_event(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "prompt": self.prompt,
            "status": self.status,
            "output": self.output,
            "tool_count": self.tool_count,
            "elapsed": round(self.elapsed, 2),
            "error": self.error,
        }


class _DenyingConfirm:
    """A drop-in handler that refuses every confirm. Used by sub-agents.

    The parent TUI's real handler would block the worker waiting on user input,
    which we cannot do inside an asyncio.gather of background tasks. Denying
    every dangerous tool is the safe default: the sub-agent's tool call still
    runs and reports the refusal as the result, which the parent agent sees.
    """

    async def __call__(self, request: Any) -> bool:  # noqa: D401 - simple callable
        return False


class SubAgentManager:
    """Spawn, supervise and report on a batch of sub-agents."""

    def __init__(self, parent: "Agent") -> None:
        self.parent = parent

    @property
    def config(self) -> Config:
        return self.parent.config

    # ----------------------------------------------------------------- public
    def validate(self, prompts: list[Any]) -> str | None:
        """Return an error string if ``prompts`` cannot be delegated."""

        if not isinstance(prompts, list) or not prompts:
            return "prompts 必须是非空字符串列表。"
        limit = self.config.subagents.max_per_call
        if len(prompts) > limit:
            return f"单次最多 {limit} 个 prompt，这次给了 {len(prompts)} 个。"
        for index, item in enumerate(prompts):
            if not isinstance(item, str) or not item.strip():
                return f"第 {index + 1} 个 prompt 不是有效字符串。"
        return None

    async def run(
        self,
        prompts: list[str],
    ) -> AsyncIterator[dict[str, Any]]:
        """Run ``prompts`` in parallel; yield lifecycle events.

        The very last event yielded is always ``subagent_summary``; its ``text``
        field is what the calling ``Agent`` should treat as the tool result.
        """

        cfg = self.config.subagents
        children: list[SubAgent] = [
            SubAgent(index=i, prompt=prompt) for i, prompt in enumerate(prompts)
        ]

        yield {
            "type": "subagent_start",
            "count": len(children),
            "prompts": [child.prompt for child in children],
        }

        semaphore = asyncio.Semaphore(max(1, cfg.max_concurrent))

        async def _supervise(child: SubAgent) -> None:
            async with semaphore:
                child.status = "running"
                child.started_at = time.monotonic()
                try:
                    output = await asyncio.wait_for(
                        self._run_one(child),
                        timeout=cfg.max_runtime,
                    )
                    child.output = output
                    child.status = "done"
                except asyncio.TimeoutError:
                    child.status = "timeout"
                    child.error = f"超过 max_runtime={cfg.max_runtime:.0f}s，已中止。"
                except asyncio.CancelledError:
                    child.status = "timeout"
                    child.error = "主任务已取消。"
                    raise
                except Exception as exc:  # noqa: BLE001
                    child.status = "error"
                    child.error = f"{type(exc).__name__}: {exc}"
                finally:
                    child.finished_at = time.monotonic()

        tasks = [
            asyncio.create_task(_supervise(child), name=f"subagent-{child.index}")
            for child in children
        ]

        partial_emitted = False
        deadline = time.monotonic() + cfg.default_timeout
        try:
            # Round 1: drive up to (and past) the default timeout.
            pending: set[asyncio.Task[None]] = set(tasks)
            while pending:
                now = time.monotonic()
                wait_for = (
                    max(0.0, deadline - now)
                    if not partial_emitted
                    else None  # any of them might finish; we wait at most a beat
                )
                done, pending = await asyncio.wait(
                    pending,
                    timeout=wait_for,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for event in self._drain_done(children):
                    yield event

                if not partial_emitted and time.monotonic() >= deadline:
                    partial_emitted = True
                    yield {
                        "type": "subagent_timeout",
                        "results": [c.to_event() for c in children],
                        "default_timeout": cfg.default_timeout,
                    }

            # Round 2: collect any stragglers that finished past the timeout.
            await asyncio.gather(*tasks, return_exceptions=True)
            for event in self._drain_done(children):
                yield event

            yield {
                "type": "subagent_final",
                "results": [c.to_event() for c in children],
            }
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        # The string the calling Agent stores as the tool result.
        yield {"type": "subagent_summary", "text": self._summarise(children)}

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _drain_done(children: list[SubAgent]):
        """Yield one ``subagent_done`` event per child that just finished."""

        for child in children:
            if child.finished_at and not child._emitted_done:
                child._emitted_done = True
                yield {
                    "type": "subagent_done",
                    "index": child.index,
                    "status": child.status,
                    "output": child.output,
                    "tool_count": child.tool_count,
                    "elapsed": round(child.elapsed, 2),
                    "error": child.error,
                }

    async def _run_one(self, child: SubAgent) -> str:
        """Drive one sub-agent to completion and return its final text."""

        agent = self._build_child_agent()
        tool_count = 0
        async for event in agent.run(child.prompt):
            kind = event.get("type")
            if kind == "tool_result":
                tool_count += 1
            elif kind == "error":
                raise RuntimeError(event.get("message", "sub-agent error"))
            elif kind == "done":
                child.tool_count = tool_count
                return self._last_assistant_text(agent)
        child.tool_count = tool_count
        return self._last_assistant_text(agent)

    @staticmethod
    def _last_assistant_text(agent: "Agent") -> str:
        for message in reversed(agent.messages):
            if message.get("role") == "assistant" and message.get("content"):
                return str(message["content"]).strip()
        return ""

    def _build_child_agent(self) -> "Agent":
        from .agent import Agent  # local import to dodge the cycle on module load

        child = Agent(
            config=self.config,
            registry=self.registry,
            model_name=self.parent.model_name,
            confirm_handler=_DenyingConfirm(),
            session_id=f"{self.parent.session_id}:sub",
            client_factory=self.parent.client_factory,
        )
        return child

    @property
    def registry(self) -> ToolRegistry:
        # The parent exposes its registry; we re-use it so sub-agents share tools.
        return self.parent.registry

    def _summarise(self, children: list[SubAgent]) -> str:
        """The final string the main Agent receives as the tool result."""

        finished = sum(1 for c in children if c.status == "done")
        total = len(children)
        head = f"[并行子任务 · {total} 个 · 完成 {finished}/{total}]"
        if not finished:
            head += " · 没有子任务成功。"

        body_lines: list[str] = []
        for child in children:
            mark = {"done": "✓", "error": "✗", "timeout": "⏱"}.get(child.status, "·")
            tools = f"{child.tool_count} 工具" if child.tool_count else "无工具"
            line = (
                f"{mark} #{child.index + 1}  {child.prompt[:60]}"
                f"  ({child.status} · {tools} · {child.elapsed:.1f}s)"
            )
            if child.status == "done" and child.output:
                body_lines.append(line + "\n" + _indent(child.output))
            elif child.error:
                body_lines.append(line + "\n" + _indent(child.error))
            else:
                body_lines.append(line)
        return head + "\n" + "\n\n".join(body_lines)


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())