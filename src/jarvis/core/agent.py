"""The agent loop: reason -> call tools -> observe -> answer.

Phase 2 adds three capabilities on top of the plain chat loop:

* long-term facts injected into the system prompt,
* plan mode (make a plan, then execute it step by step),
* a distiller that turns finished conversations into durable facts.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from ..config import Config
from ..llm.client import LLMClient, LLMError, StreamResult
from .planner import PlanStep, extract_json, parse_plan, step_instruction
from .prompts import LEARN_PROMPT, PLANNER_PROMPT, plan_request, system_prompt
from .registry import Tool, ToolRegistry

_QUEUE_END = object()

FactsProvider = Callable[[], list[str]]


@dataclass
class ConfirmRequest:
    """A dangerous tool call waiting for the user's blessing."""

    tool: str
    arguments: dict[str, Any]
    hint: str = ""
    preview: str = ""

    @property
    def summary(self) -> str:
        if self.preview:
            return self.preview
        if not self.arguments:
            return self.tool
        rendered = ", ".join(f"{k}={v!r}" for k, v in self.arguments.items())
        return f"{self.tool}({rendered})"


ConfirmHandler = Callable[[ConfirmRequest], Awaitable[bool]]


@dataclass
class Agent:
    """Stateful conversation driver with tool execution."""

    config: Config
    registry: ToolRegistry
    model_name: str | None = None
    confirm_handler: ConfirmHandler | None = None
    session_id: str = "default"
    messages: list[dict[str, Any]] = field(default_factory=list)
    on_message: Callable[[dict[str, Any]], None] | None = None
    facts_provider: FactsProvider | None = None
    plan: list[PlanStep] = field(default_factory=list)
    plan_goal: str = ""
    _client: LLMClient | None = None
    _last_result: StreamResult | None = None

    def __post_init__(self) -> None:
        self.reset()

    # --------------------------------------------------------------- lifecycle
    def facts(self) -> list[str]:
        if self.facts_provider is None:
            return []
        try:
            return list(self.facts_provider())[: self.config.memory.max_facts_in_prompt]
        except Exception:  # noqa: BLE001 - memory must never break a turn
            return []

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": system_prompt(self.facts())}]
        self.plan = []
        self.plan_goal = ""

    def refresh_system(self) -> None:
        """Rebuild the system message (after facts changed or a model switch)."""

        content = system_prompt(self.facts())
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = content
        else:
            self.messages.insert(0, {"role": "system", "content": content})

    @property
    def model(self):
        return self.config.model(self.model_name)

    def set_model(self, name: str) -> None:
        self.config.model(name)  # validation
        self.model_name = name
        self._client = None

    @property
    def client(self) -> LLMClient:
        if self._client is None:
            self._client = LLMClient(self.model)
        return self._client

    def drop_client(self) -> None:
        self._client = None

    async def aclose(self) -> None:
        """Close the HTTP pool held by the current client."""

        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ history
    def history(self) -> list[dict[str, Any]]:
        return [m for m in self.messages if m.get("role") != "system"]

    def load_history(self, messages: list[dict[str, Any]]) -> None:
        self.reset()
        limit = self.config.security.history_limit
        for item in messages[-limit:]:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and content:
                self.messages.append({"role": role, "content": content})

    def transcript(self, turns: int = 6) -> str:
        """Recent user/assistant text, for the memory distiller."""

        lines: list[str] = []
        for message in self.history()[-turns * 2 :]:
            role = message.get("role")
            content = message.get("content")
            if role in {"user", "assistant"} and content:
                speaker = "用户" if role == "user" else "JARVIS"
                lines.append(f"{speaker}: {str(content)[:1200]}")
        return "\n\n".join(lines)

    def _remember(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        if self.on_message is not None and message.get("role") != "system":
            self.on_message(message)

    # --------------------------------------------------------------------- run
    async def run(self, user_text: str) -> AsyncIterator[dict[str, Any]]:
        """Yield UI events while handling one user turn."""

        self._remember({"role": "user", "content": user_text})

        for _round in range(self.config.security.max_tool_rounds + 1):
            async for event in self._iterate(self.messages, self.registry.definitions()):
                yield event

            result = self._last_result
            if result is None:
                return
            if not result.tool_calls:
                if result.content.strip():
                    self._remember({"role": "assistant", "content": result.content})
                yield {"type": "done"}
                return

            self._remember(
                {
                    "role": "assistant",
                    "content": result.content or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments},
                        }
                        for call in result.tool_calls
                    ],
                }
            )

            for call in result.tool_calls:
                tool = self.registry.get(call.name)
                arguments = self._parse_arguments(call.arguments)
                output: str

                if tool is None:
                    output = f"[错误] 不存在名为 {call.name} 的工具。"
                else:
                    if self.registry.needs_confirmation(tool):
                        request = ConfirmRequest(
                            tool=tool.name,
                            arguments=arguments,
                            hint=tool.hint,
                            preview=self._preview(tool, arguments),
                        )
                        yield {"type": "confirm", "request": request}
                        approved = True
                        if self.confirm_handler is not None:
                            approved = await self.confirm_handler(request)
                        if not approved:
                            output = "[已拒绝] 用户拒绝执行该操作，请换一种方式或向用户说明。"
                            self._remember(
                                {"role": "tool", "tool_call_id": call.id, "content": output}
                            )
                            yield {
                                "type": "tool_result",
                                "name": tool.name,
                                "output": output,
                                "ok": False,
                            }
                            continue

                    yield {"type": "tool_start", "name": call.name, "arguments": arguments}
                    output = await self.registry.call(call.name, arguments)
                    yield {
                        "type": "tool_result",
                        "name": call.name,
                        "output": output,
                        "ok": not output.startswith("[错误]"),
                    }

                self._remember({"role": "tool", "tool_call_id": call.id, "content": output})

        yield {
            "type": "error",
            "message": f"已达到工具调用上限（{self.config.security.max_tool_rounds} 轮），先停下来向你汇报。",
        }

    # --------------------------------------------------------------- plan mode
    async def plan_task(self, task: str) -> AsyncIterator[dict[str, Any]]:
        """Ask the model for an execution plan (no tools, JSON only)."""

        yield {"type": "notice", "text": f"正在为「{task}」制定计划…"}
        messages = [
            {"role": "system", "content": PLANNER_PROMPT},
            {"role": "user", "content": plan_request(task)},
        ]
        text = ""
        async for event in self._iterate(messages, None):
            if event["type"] == "error":
                yield event
                return
            text += event.get("text", "")

        steps = parse_plan(text)
        if not steps:
            yield {
                "type": "error",
                "message": "模型没有给出可解析的计划。原始回复：\n" + (text.strip()[:600] or "(空)"),
            }
            return

        self.plan = steps
        self.plan_goal = task
        yield {
            "type": "plan",
            "task": task,
            "steps": [step.to_dict() for step in steps],
        }

    async def execute_plan(self) -> AsyncIterator[dict[str, Any]]:
        """Run the current plan, one step at a time."""

        if not self.plan:
            yield {"type": "error", "message": "当前没有计划，先用 /plan <任务> 生成一个。"}
            return

        total = len(self.plan)
        for index, step in enumerate(self.plan):
            step.status = "running"
            yield {
                "type": "step_start",
                "index": index,
                "total": total,
                "title": step.title,
            }
            failed = False
            async for event in self.run(step_instruction(index, total, step)):
                if event["type"] == "error":
                    failed = True
                yield event
            step.status = "failed" if failed else "done"
            yield {"type": "step_done", "index": index, "status": step.status}

        done = sum(1 for step in self.plan if step.status == "done")
        yield {"type": "plan_done", "done": done, "total": total}

    # ------------------------------------------------------------- memory distill
    async def learn(self, transcript: str) -> list[str]:
        """Extract durable facts from a conversation snippet (empty on failure)."""

        if not transcript.strip():
            return []
        messages = [
            {"role": "system", "content": LEARN_PROMPT},
            {"role": "user", "content": transcript[:8000]},
        ]
        text = ""
        async for event in self._iterate(messages, None):
            if event["type"] == "error":
                return []
            text += event.get("text", "")

        data = extract_json(text)
        if not isinstance(data, list):
            return []
        facts: list[str] = []
        for item in data:
            if isinstance(item, str) and item.strip():
                facts.append(" ".join(item.split())[:300])
        return facts

    # ------------------------------------------------------------------ streaming
    async def _iterate(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream one completion, yielding text events; result lands in _last_result."""

        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(
            self.client.stream(
                messages,
                tools=tools,
                on_delta=lambda text: queue.put_nowait(text),
            )
        )
        task.add_done_callback(lambda _t: queue.put_nowait(_QUEUE_END))

        self._last_result = None
        while True:
            item = await queue.get()
            if item is _QUEUE_END:
                break
            yield {"type": "text", "text": item}

        try:
            self._last_result = await task
        except LLMError as exc:
            yield {"type": "error", "message": str(exc)}
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _parse_arguments(raw: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _preview(tool: Tool, arguments: dict[str, Any]) -> str:
        if tool.name == "run_shell" and "command" in arguments:
            return str(arguments["command"])
        if tool.name == "write_file" and "path" in arguments:
            content = str(arguments.get("content", ""))
            return f"写入 {arguments['path']}（{len(content)} 字符）"
        if not arguments:
            return tool.name
        return f"{tool.name} " + ", ".join(f"{k}={v!r}" for k, v in arguments.items())
