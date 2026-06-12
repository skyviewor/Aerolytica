"""LLM client with OpenAI-compatible API.

Supports DeepSeek, OpenAI, Ollama, and any OpenAI-compatible endpoint.
Includes both non-streaming and streaming (SSE) chat.
"""

import json
import re
import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import httpx
import structlog

from aero.core.types import Message, ToolCall

logger = structlog.get_logger()

_TRANSIENT_RETRIES = 1
_TRANSIENT_HTTP_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
)


class StreamEvent:
    """A single event from a streaming LLM response."""

    def __init__(self, type: str, content: str = "", tool_call: ToolCall | None = None, usage: dict | None = None):
        self.type = type  # "text" | "tool_call" | "done"
        self.content = content
        self.tool_call = tool_call
        self.usage = usage


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    reasoning_effort: str = ""
    api_key: str = ""
    base_url: str = ""

    @property
    def endpoint(self) -> str:
        if self.base_url:
            base_url = self.base_url.rstrip("/")
            if base_url.endswith("/v1"):
                return base_url + "/chat/completions"
            return base_url + "/v1/chat/completions"
        if self.provider == "deepseek":
            return "https://api.deepseek.com/v1/chat/completions"
        if self.provider == "openai":
            return "https://api.openai.com/v1/chat/completions"
        if self.provider == "ollama":
            return "http://localhost:11434/v1/chat/completions"
        if self.provider == "bailian":
            return "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        if self.provider == "kimi":
            return "https://api.moonshot.cn/v1/chat/completions"
        return self.base_url or "https://api.deepseek.com/v1/chat/completions"


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=120)
        self.last_usage: dict | None = None

    async def close(self):
        await self._client.aclose()

    async def chat(self, messages: list[Message]) -> str:
        """Send messages to LLM and return text response."""
        response = await self._send(messages, tools=None)
        self.last_usage = response.get("usage")
        choice = response.get("choices", [{}])[0]
        return choice.get("message", {}).get("content", "")

    async def chat_stream(self, messages: list[Message]) -> AsyncGenerator[StreamEvent, None]:
        """Stream text tokens from LLM."""
        headers = self._headers()
        body = self._request_body(messages, stream=True)

        logger.info("llm.stream", model=self.config.model)

        for attempt in range(_TRANSIENT_RETRIES + 1):
            emitted = False
            captured_usage: dict | None = None
            try:
                async with self._client.stream(
                    "POST", self.config.endpoint, json=body, headers=headers
                ) as response:
                    await _raise_for_status_stream(response)
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line.removeprefix("data: ")
                        if data_str == "[DONE]":
                            self.last_usage = captured_usage
                            yield StreamEvent(type="done", usage=captured_usage)
                            return
                        try:
                            data = json.loads(data_str)
                            if "usage" in data:
                                captured_usage = data["usage"]
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                emitted = True
                                yield StreamEvent(type="text", content=content)
                        except json.JSONDecodeError:
                            continue
            except _TRANSIENT_HTTP_ERRORS as e:
                if emitted:
                    raise _stream_interrupted_error(e) from e
                if attempt < _TRANSIENT_RETRIES:
                    logger.warning("llm.stream.retry", error=repr(e), attempt=attempt + 1)
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                raise _connection_error(e) from e

    async def chat_with_tools(
        self, messages: list[Message], tools: list[dict]
    ) -> tuple[str, list[ToolCall]]:
        """Send messages to LLM with tool definitions, return text + tool calls."""
        response = await self._send(messages, tools=tools)
        self.last_usage = response.get("usage")
        choice = response.get("choices", [{}])[0]
        msg = choice.get("message", {})

        text = msg.get("content") or ""
        tool_calls_raw = msg.get("tool_calls") or []
        tool_calls = [
            ToolCall(
                id=tc.get("id", f"call_{i}"),
                name=tc["function"]["name"],
                arguments=_parse_args(tc["function"].get("arguments", "")),
            )
            for i, tc in enumerate(tool_calls_raw)
        ]
        if not tool_calls:
            text, tool_calls = _parse_content_tool_calls(text)

        return text, tool_calls

    async def chat_with_tools_stream(
        self, messages: list[Message], tools: list[dict]
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream LLM response with tool calls.

        Yields StreamEvent with type "text" for tokens, "tool_call" for tool calls,
        and "done" when the stream ends.
        """
        headers = self._headers()
        body = self._request_body(messages, tools=tools, stream=True)

        logger.info("llm.stream_tools", model=self.config.model, tool_count=len(tools))

        for attempt in range(_TRANSIENT_RETRIES + 1):
            emitted = False
            tool_calls_buffer: dict[int, dict] = {}
            content_buffer = ""
            content_sent = 0
            captured_usage: dict | None = None
            try:
                async with self._client.stream(
                    "POST", self.config.endpoint, json=body, headers=headers
                ) as response:
                    await _raise_for_status_stream(response)
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line.removeprefix("data: ")
                        if data_str == "[DONE]":
                            self.last_usage = captured_usage
                            content_text, content_tool_calls = _parse_content_tool_calls(content_buffer)
                            if len(content_text) > content_sent:
                                emitted = True
                                yield StreamEvent(
                                    type="text",
                                    content=content_text[content_sent:],
                                )
                            if content_tool_calls:
                                for tc in content_tool_calls:
                                    emitted = True
                                    yield StreamEvent(type="tool_call", tool_call=tc)
                                yield StreamEvent(type="done", usage=captured_usage)
                                return

                            for idx in sorted(tool_calls_buffer.keys()):
                                buf = tool_calls_buffer[idx]
                                fn_name = buf.get("function", {}).get("name", "")
                                fn_args = buf.get("function", {}).get("arguments", "")
                                emitted = True
                                yield StreamEvent(
                                    type="tool_call",
                                    tool_call=ToolCall(
                                        id=buf.get("id", f"call_{idx}"),
                                        name=fn_name,
                                        arguments=fn_args,
                                    ),
                                )
                            yield StreamEvent(type="done", usage=captured_usage)
                            return
                        try:
                            data = json.loads(data_str)
                            if "usage" in data:
                                captured_usage = data["usage"]
                            delta = data.get("choices", [{}])[0].get("delta", {})
                        except json.JSONDecodeError:
                            continue

                        content = delta.get("content", "")
                        if content:
                            content_buffer += content
                            marker_start = _find_tool_call_marker_start(content_buffer)
                            if marker_start is not None:
                                if marker_start > content_sent:
                                    emitted = True
                                    yield StreamEvent(
                                        type="text",
                                        content=content_buffer[content_sent:marker_start],
                                    )
                                    content_sent = marker_start
                            elif not tool_calls_buffer:
                                safe_end = _safe_content_stream_end(content_buffer, content_sent)
                                if safe_end > content_sent:
                                    emitted = True
                                    yield StreamEvent(
                                        type="text",
                                        content=content_buffer[content_sent:safe_end],
                                    )
                                    content_sent = safe_end

                        tc_deltas = delta.get("tool_calls") or []
                        for tc_delta in tc_deltas:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_calls_buffer:
                                tool_calls_buffer[idx] = {
                                    "id": tc_delta.get("id", ""),
                                    "function": {"name": "", "arguments": ""},
                                }
                            buf = tool_calls_buffer[idx]
                            if "id" in tc_delta:
                                buf["id"] = tc_delta["id"]
                            fn = tc_delta.get("function", {})
                            if "name" in fn:
                                buf["function"]["name"] += fn["name"]
                            if "arguments" in fn:
                                buf["function"]["arguments"] += fn["arguments"]
            except _TRANSIENT_HTTP_ERRORS as e:
                if emitted:
                    raise _stream_interrupted_error(e) from e
                if attempt < _TRANSIENT_RETRIES:
                    logger.warning("llm.stream_tools.retry", error=repr(e), attempt=attempt + 1)
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                raise _connection_error(e) from e

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    async def _send(
        self, messages: list[Message], tools: list[dict] | None
    ) -> dict:
        headers = self._headers()
        body = self._request_body(messages, tools=tools, stream=False)

        logger.info("llm.request", model=self.config.model, tool_count=len(tools or []))

        for attempt in range(_TRANSIENT_RETRIES + 1):
            try:
                resp = await self._client.post(self.config.endpoint, json=body, headers=headers)
                _raise_for_status(resp)
                return resp.json()
            except _TRANSIENT_HTTP_ERRORS as e:
                if attempt < _TRANSIENT_RETRIES:
                    logger.warning("llm.request.retry", error=repr(e), attempt=attempt + 1)
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                raise _connection_error(e) from e
        raise RuntimeError("模型服务连接异常，请稍后重试。")

    def _request_body(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        stream: bool = False,
    ) -> dict:
        body = {
            "model": self.config.model,
            "messages": [self._format_msg(m) for m in messages],
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if self.config.reasoning_effort:
            body["reasoning_effort"] = self.config.reasoning_effort
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    @staticmethod
    def _format_msg(m: Message) -> dict:
        msg: dict = {"role": m.role, "content": m.content}
        if m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        if m.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": (
                            json.dumps(tc.arguments, ensure_ascii=False)
                            if isinstance(tc.arguments, dict)
                            else tc.arguments
                        ),
                    },
                }
                for tc in m.tool_calls
            ]
        return msg


def _parse_args(arguments: str | dict) -> dict:
    if isinstance(arguments, dict):
        return arguments
    try:
        return json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return {}


def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 401:
            raise RuntimeError(
                "LLM API 未授权（401）：当前模型服务商的 API key 无效或不匹配。"
            ) from e
        if status == 429:
            raise RuntimeError("LLM API 请求过快或额度不足（429），请稍后再试。") from e
        if status == 400:
            body = e.response.text
            if "Content Exists Risk" in body:
                raise RuntimeError(
                    "当前对话内容被模型服务商的安全策略拦截（Content Exists Risk）。\n"
                    "建议换一种表述方式重试，或使用 /provider 切换到其他服务商（如阿里云百炼、Kimi）。"
                ) from e
            if "content_filter" in body.lower() or "safety" in body.lower():
                raise RuntimeError(
                    "当前对话内容触发了模型服务商的内容过滤策略。请尝试换一种说法，"
                    "或用 /provider 切换到其他服务商。"
                ) from e
        if 500 <= status < 600:
            raise RuntimeError(f"LLM 服务暂时不可用（{status}），请稍后再试。") from e
        raise RuntimeError(f"LLM API 请求失败（HTTP {status}）：{e.response.text}") from e


async def _raise_for_status_stream(response: httpx.Response) -> None:
    if response.is_error:
        await response.aread()
    _raise_for_status(response)


def _connection_error(error: BaseException) -> RuntimeError:
    return RuntimeError(
        "模型服务连接中断，已自动重试但仍未恢复。"
        "这通常是模型服务商或网络网关的临时断连，不是当前数据或绘图步骤本身失败。"
        "请稍后重试，或用 /provider 切换到其他模型服务商。"
    )


def _stream_interrupted_error(error: BaseException) -> RuntimeError:
    return RuntimeError(
        "模型服务在回复过程中断开连接。前面的工具步骤可能已经执行完成，"
        "但最终回复没有生成完整。请直接重试刚才的请求。"
    )


def _parse_content_tool_calls(content: str) -> tuple[str, list[ToolCall]]:
    """Parse provider-specific text tool-call markup when native tool_calls are absent."""
    normalized_content = _normalize_tool_markup(content)
    if "tool_calls" not in normalized_content and "tool▁calls" not in normalized_content:
        return content, []

    clean_text = _strip_tool_call_markup(normalized_content)
    tool_calls = _parse_dsml_tool_calls(normalized_content) or _parse_deepseek_tool_calls(
        normalized_content
    )
    return clean_text, tool_calls


def _normalize_tool_markup(content: str) -> str:
    return re.sub(
        r"<\s*(?P<slash>/?)\s*｜\s*\|\s*DSML\s*\|\s*\|\s*(?P<tag>[A-Za-z_][\w]*)",
        r"<\g<slash>｜DSML｜\g<tag>",
        content,
    )


def _strip_tool_call_markup(content: str) -> str:
    marker_start = _find_tool_call_marker_start(content)
    if marker_start is not None:
        return content[:marker_start].rstrip()
    return content


def _find_tool_call_marker_start(content: str) -> int | None:
    marker = re.search(r"<[^>\n]*(?:tool_calls|tool▁calls)[^>\n]*>", content)
    if marker:
        return marker.start()
    return None


def _safe_content_stream_end(content: str, already_sent: int) -> int:
    """Return how much text can be streamed without leaking a split tool marker."""
    unsent = content[already_sent:]
    possible_marker_start = unsent.rfind("<")
    if possible_marker_start == -1:
        return len(content)

    marker_tail = unsent[possible_marker_start:]
    marker_probe = marker_tail[1:].lstrip()
    if not marker_probe.startswith(("｜", "/｜")):
        return len(content)
    if len(marker_tail) < 32:
        return already_sent + possible_marker_start
    return len(content)


def _parse_dsml_tool_calls(content: str) -> list[ToolCall]:
    calls: list[ToolCall] = []
    invoke_pattern = re.compile(
        r"<[^>\n]*invoke\s+name=[\"'](?P<name>[^\"']+)[\"'][^>]*>"
        r"(?P<body>.*?)"
        r"</[^>\n]*invoke\s*>",
        re.DOTALL,
    )
    parameter_pattern = re.compile(
        r"<[^>\n]*parameter\s+name=[\"'](?P<name>[^\"']+)[\"'][^>]*>"
        r"(?P<value>.*?)"
        r"</[^>\n]*parameter\s*>",
        re.DOTALL,
    )

    for index, match in enumerate(invoke_pattern.finditer(content)):
        args: dict = {}
        for param in parameter_pattern.finditer(match.group("body")):
            value = param.group("value").strip()
            args[param.group("name")] = _parse_tool_value(value)
        calls.append(
            ToolCall(id=f"content_call_{index}", name=match.group("name"), arguments=args)
        )
    return calls


def _parse_deepseek_tool_calls(content: str) -> list[ToolCall]:
    calls: list[ToolCall] = []
    pattern = re.compile(
        r"(?:tool_call_begin|tool▁call▁begin).*?"
        r"(?:tool_sep|tool▁sep)[^A-Za-z_]*"
        r"(?P<name>[A-Za-z_][\w]*)"
        r"(?P<body>.*?)"
        r"(?:tool_call_end|tool▁call▁end)",
        re.DOTALL,
    )

    for index, match in enumerate(pattern.finditer(content)):
        json_match = re.search(
            r"```(?:json)?\s*(?P<json>{.*?})\s*```",
            match.group("body"),
            re.DOTALL,
        )
        args = _parse_tool_value(json_match.group("json")) if json_match else {}
        calls.append(
            ToolCall(id=f"content_call_{index}", name=match.group("name"), arguments=args)
        )
    return calls


def _parse_tool_value(value: str):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
