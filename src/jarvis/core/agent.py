"""The agent loop: reason -> call tools -> observe -> answer."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from ..config import Config
from ..llm.client import LLMClient, LLMError
from .prompts import system_prompt
from .registry import Tool, ToolRegistry

_QUEUE_END = object()


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
    _client: LLMClient | None = None

    def __post_init__(self) -> None:
        self.reset()

    # --------------------------------------------------------------- lifecycle
    def reset(self) -> None:
        self.messages = [{"role": "system", "content": system_prompt()}]

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

    def _remember(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        if self.on_message is not None and message.get("role") != "system":
            self.on_message(message)

    # --------------------------------------------------------------------- run
    async def run(self, user_text: str) -> AsyncIterator[dict[str, Any]]:
        """Yield UI events while handling one user turn."""

        self._remember({"role": "user", "content": user_text})

        for _round in range(self.config.security.max_tool_rounds + 1):
            queue: asyncio.Queue = asyncio.Queue()

            task = asyncio.create_task(
                self.client.stream(
                    self.messages,
                    tools=self.registry.definitions(),
                    on_delta=lambda text: queue.put_nowait(text),
                )
            )
            task.add_done_callback(lambda _t: queue.put_nowait(_QUEUE_END))

            while True:
                item = await queue.get()
                if item is _QUEUE_END:
                    break
                yield {"type": "text", "text": item}

            try:
                result = await task
            except LLMError as exc:
                yield {"type": "error", "message": str(exc)}
                return
            except asyncio.CancelledError:
                task.cancel()
                raise
            except Exception as exc:  # noqa: BLE001
                yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
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
        return f"{tool.name} " + ", ".join(f"{k}={v!r}" for k, v in arguments.items())
