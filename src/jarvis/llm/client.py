"""OpenAI-compatible chat client with streaming and tool calling."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from openai import AsyncOpenAI

from ..config import ModelConfig


class LLMError(RuntimeError):
    """Any failure while talking to the model endpoint."""


async def _aclose(stream: Any) -> None:
    """Best-effort close for streaming responses across openai versions."""

    closer = getattr(stream, "close", None)
    if closer is None:
        return
    try:
        outcome = closer()
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:  # noqa: BLE001 - cleanup must never mask real errors
        pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class StreamResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    degraded: bool = False


class LLMClient:
    """Thin wrapper that always streams and tolerates endpoints without tools."""

    def __init__(self, cfg: ModelConfig) -> None:
        self.cfg = cfg
        api_key = cfg.resolve_api_key()
        if not api_key and "localhost" not in cfg.base_url and "127.0.0.1" not in cfg.base_url:
            raise LLMError(
                f"模型 '{cfg.display}' 未配置 API Key。\n"
                f"请在 config.toml 的 [models.{cfg.name}] 里填写 api_key，"
                f"或设置环境变量 {cfg.api_key_env or 'API_KEY'}。"
            )
        self.client = AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=api_key or "not-needed",
            timeout=180.0,
            max_retries=1,
        )
        self.tools_supported = cfg.supports_tools

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> StreamResult:
        """Stream one completion, forwarding text deltas to ``on_delta``."""

        try:
            return await self._stream_once(messages, tools, on_delta)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if tools and self._looks_like_tool_issue(message):
                # Endpoint rejected the tool schema - retry as plain chat.
                self.tools_supported = False
                result = await self._stream_once(messages, None, on_delta)
                result.content = (
                    "[提示] 当前模型不支持工具调用，已降级为纯对话模式。\n\n" + result.content
                )
                result.degraded = True
                return result
            raise LLMError(f"模型请求失败：{message}") from exc

    @staticmethod
    def _looks_like_tool_issue(message: str) -> bool:
        lowered = message.lower()
        return any(
            key in lowered
            for key in ("tool", "function", "tools is not supported", "unsupported parameter")
        )

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool."""

        try:
            await self.client.close()
        except Exception:  # noqa: BLE001
            pass

    async def _stream_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        on_delta: Callable[[str], None] | None,
    ) -> StreamResult:
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "stream": True,
        }
        if tools and self.tools_supported:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        stream = await self.client.chat.completions.create(**kwargs)
        result = StreamResult()

        buffers: dict[int, dict[str, str]] = {}
        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if getattr(delta, "content", None):
                    result.content += delta.content
                    if on_delta is not None:
                        on_delta(delta.content)
                for call in getattr(delta, "tool_calls", None) or []:
                    slot = buffers.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                    if call.id:
                        slot["id"] = call.id
                    if call.function is not None:
                        if call.function.name:
                            slot["name"] += call.function.name
                        if call.function.arguments:
                            slot["arguments"] += call.function.arguments
                if choice.finish_reason:
                    result.finish_reason = choice.finish_reason
        finally:
            # Close the SSE response so httpcore does not complain at shutdown.
            await _aclose(stream)

        for index in sorted(buffers):
            slot = buffers[index]
            if slot["name"]:
                result.tool_calls.append(
                    ToolCall(
                        id=slot["id"] or f"call_{index}",
                        name=slot["name"],
                        arguments=slot["arguments"] or "{}",
                    )
                )
        return result
