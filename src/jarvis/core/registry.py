"""Tool registry: schema definition, dispatch and output truncation."""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import SecurityConfig


class ToolError(RuntimeError):
    """Raised when a tool refuses to run or fails."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    dangerous: bool = False
    hint: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 4 :]
    return f"{head}\n... [输出过长，已截断 {len(text) - len(head) - len(tail)} 字符] ...\n{tail}"


class ToolRegistry:
    """Holds every tool the agent may call."""

    def __init__(self, security: SecurityConfig) -> None:
        self.security = security
        self._tools: dict[str, Tool] = {}

    # ------------------------------------------------------------------ setup
    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        *,
        dangerous: bool = False,
        hint: str = "",
        tags: list[str] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.register(
                Tool(
                    name=name,
                    description=description,
                    parameters=parameters,
                    func=func,
                    dangerous=dangerous,
                    hint=hint,
                    tags=list(tags or []),
                )
            )
            return func

        return decorator

    # ------------------------------------------------------------- inspection
    @property
    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> list[dict[str, Any]]:
        return [t.schema for t in self._tools.values()]

    def needs_confirmation(self, tool: Tool) -> bool:
        if not tool.dangerous:
            return False
        for pattern in self.security.auto_approve:
            if fnmatch.fnmatch(tool.name, pattern):
                return False
        return True

    # ---------------------------------------------------------------- execute
    async def call(self, name: str, arguments: str | dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"[错误] 不存在名为 {name} 的工具。可用工具：{', '.join(self._tools)}"

        if isinstance(arguments, str):
            raw = arguments.strip() or "{}"
            try:
                kwargs = json.loads(raw)
            except json.JSONDecodeError as exc:
                return f"[错误] 参数不是合法 JSON：{exc}。收到的内容：{raw[:200]}"
        else:
            kwargs = dict(arguments)
        if not isinstance(kwargs, dict):
            return "[错误] 参数必须是 JSON 对象。"

        try:
            if inspect.iscoroutinefunction(tool.func):
                result = await tool.func(**kwargs)
            else:
                result = await asyncio.to_thread(tool.func, **kwargs)
        except TypeError as exc:
            return f"[错误] 参数不匹配：{exc}"
        except ToolError as exc:
            return f"[错误] {exc}"
        except Exception as exc:  # noqa: BLE001 - surface any tool failure to the model
            return f"[错误] {type(exc).__name__}: {exc}"

        if result is None:
            return "[完成] 无返回值"
        return _truncate(str(result), self.security.max_tool_output)
