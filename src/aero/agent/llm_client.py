"""LLM client using OpenAI-compatible Chat Completions for every provider."""

import asyncio
import json
import re
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timezone

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
_DASHSCOPE_GENERATION_PATH = "/api/v1/services/aigc/text-generation/generation"
_DASHSCOPE_MULTIMODAL_GENERATION_PATH = (
    "/api/v1/services/aigc/multimodal-generation/generation"
)


def _first_choice(response: dict) -> dict:
    """Return the first valid choice, tolerating usage-only API responses."""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return {}
    choice = choices[0]
    return choice if isinstance(choice, dict) else {}


class StreamEvent:
    """A single event from a streaming LLM response."""

    def __init__(
        self,
        type: str,
        content: str = "",
        tool_call: ToolCall | None = None,
        usage: dict | None = None,
        references: list[str] | None = None,
    ):
        self.type = type  # "text" | "tool_call" | "references" | "done"
        self.content = content
        self.tool_call = tool_call
        self.usage = usage
        self.references = references or []


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    reasoning_effort: str = ""
    api_key: str = ""
    base_url: str = ""
    max_tokens: int | None = None

    @property
    def endpoint(self) -> str:
        if self.provider == "official":
            from aero.core.official_account import relay_llm_url

            return relay_llm_url() + "/chat/completions"
        if self.base_url:
            base_url = self.base_url.rstrip("/")
            if base_url.endswith(("/v1", "/v4")):
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

    def _dashscope_endpoint(self, path: str) -> str:
        """Return a native DashScope endpoint while preserving a custom origin.

        A custom Bailian OpenAI-compatible URL can be a regional endpoint.  In
        that case preserve its origin/workspace prefix while replacing the
        compatible-mode suffix with the native API path.
        """
        if self.base_url:
            base = self.base_url.rstrip("/")
            marker = "/compatible-mode"
            if marker in base:
                return base.split(marker, 1)[0] + path
        return "https://dashscope.aliyuncs.com" + path

    @property
    def dashscope_generation_endpoint(self) -> str:
        """Return DashScope's native text-generation endpoint."""
        return self._dashscope_endpoint(_DASHSCOPE_GENERATION_PATH)

    @property
    def dashscope_multimodal_generation_endpoint(self) -> str:
        """Return DashScope's native multimodal-generation endpoint."""
        return self._dashscope_endpoint(_DASHSCOPE_MULTIMODAL_GENERATION_PATH)


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=120)
        self._official_session = None
        self.relay_turn_id = ""
        self.last_usage: dict | None = None
        self.last_search_references: list[str] = []
        self.last_search_performed = False
        self.last_request_started_at: datetime | None = None
        self.last_request_id = ""

    async def close(self):
        await self._client.aclose()
        if self._official_session is not None:
            await self._official_session.close()

    async def _request_headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        if self.config.provider != "official":
            return self._headers()
        if self._official_session is None:
            from aero.core.official_account import OfficialAccountSession

            self._official_session = OfficialAccountSession()
        token = await self._official_session.access_token(force_refresh=force_refresh)
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _identify_request(self, headers: dict[str, str]) -> None:
        if self.config.provider == "official":
            self.last_request_id = uuid.uuid4().hex
            headers["Idempotency-Key"] = self.last_request_id

    async def _official_transport_failure(self, headers: dict[str, str]) -> None:
        """An ambiguous billable request must never be blindly replayed."""
        if self.config.provider != "official":
            return
        key = headers["Idempotency-Key"]
        status = "unknown"
        try:
            from aero.core.official_account import relay_llm_url

            url = relay_llm_url() + f"/requests/{key}"
            result = await self._client.get(url, headers=await self._request_headers())
            if result.status_code == 200:
                status = str(result.json().get("status", "unknown"))
        except (httpx.HTTPError, ValueError):
            pass
        raise RuntimeError(
            f"官方模型连接中断（请求 {key}，状态 {status}）。"
            "为避免重复执行或扣费，未自动重发；请查看官方额度明细后决定是否重新运行。"
        )

    async def chat(self, messages: list[Message]) -> str:
        """Send messages to LLM and return text response."""
        response = await self._send(messages, tools=None)
        self.last_usage = response.get("usage")
        choice = _first_choice(response)
        return choice.get("message", {}).get("content", "")

    async def chat_stream(self, messages: list[Message]) -> AsyncGenerator[StreamEvent, None]:
        """Stream text tokens from LLM."""
        self.last_search_references = []
        self.last_search_performed = False
        headers = await self._request_headers()
        self._identify_request(headers)
        body = self._request_body(messages, stream=True)

        logger.info("llm.stream", model=self.config.model)

        for attempt in range(_TRANSIENT_RETRIES + 1):
            emitted = False
            captured_usage: dict | None = None
            try:
                self.last_request_started_at = datetime.now(timezone.utc)
                async with self._client.stream(
                    "POST", self.config.endpoint, json=body, headers=headers
                ) as response:
                    if response.status_code == 401 and self.config.provider == "official":
                        if attempt < _TRANSIENT_RETRIES:
                            headers.update(await self._request_headers(force_refresh=True))
                            continue
                        raise RuntimeError(
                            "Aerolytica 官方账户登录已失效，请使用 /login 重新登录。"
                        )
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
                            delta = _first_choice(data).get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                emitted = True
                                yield StreamEvent(type="text", content=content)
                        except json.JSONDecodeError:
                            continue
            except _TRANSIENT_HTTP_ERRORS as e:
                if self.config.provider == "official":
                    await self._official_transport_failure(headers)
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
        choice = _first_choice(response)
        msg = choice.get("message", {})

        text = msg.get("content") or ""
        tool_calls_raw = msg.get("tool_calls") or []
        tool_calls = [
            ToolCall(
                id=tc.get("id") or f"call_{i}",
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
        self.last_search_references = []
        self.last_search_performed = False
        headers = await self._request_headers()
        self._identify_request(headers)
        body = self._request_body(messages, tools=tools, stream=True)

        logger.info("llm.stream_tools", model=self.config.model, tool_count=len(tools))

        for attempt in range(_TRANSIENT_RETRIES + 1):
            emitted = False
            tool_calls_buffer: dict[int, dict] = {}
            content_buffer = ""
            content_sent = 0
            captured_usage: dict | None = None
            try:
                self.last_request_started_at = datetime.now(timezone.utc)
                async with self._client.stream(
                    "POST", self.config.endpoint, json=body, headers=headers
                ) as response:
                    if response.status_code == 401 and self.config.provider == "official":
                        if attempt < _TRANSIENT_RETRIES:
                            headers.update(await self._request_headers(force_refresh=True))
                            continue
                        raise RuntimeError(
                            "Aerolytica 官方账户登录已失效，请使用 /login 重新登录。"
                        )
                    await _raise_for_status_stream(response)
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line.removeprefix("data: ")
                        if data_str == "[DONE]":
                            self.last_usage = captured_usage
                            content_text, content_tool_calls = _parse_content_tool_calls(
                                content_buffer
                            )
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
                                        id=buf.get("id") or f"call_{idx}",
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
                            delta = _first_choice(data).get("delta", {})
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
                                safe_end = _safe_content_stream_end(
                                    content_buffer, content_sent
                                )
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
                if self.config.provider == "official":
                    await self._official_transport_failure(headers)
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
            # Some provider/CDN routes have returned invalid Brotli frames.  Model
            # responses are small enough that avoiding content encoding is safer.
            "Accept-Encoding": "identity",
        }

    def _dashscope_headers(self, *, stream: bool = False) -> dict:
        headers = self._headers()
        if stream:
            # DashScope native streaming is enabled through this HTTP header,
            # not an OpenAI ``stream`` field in the request body.
            headers["X-DashScope-SSE"] = "enable"
        return headers

    def _uses_dashscope_native_search(self) -> bool:
        """Native model-side search was removed; all chat uses OpenAI format."""
        return False

    def _uses_dashscope_multimodal_search(self) -> bool:
        """Whether the current native-search call needs the multimodal API."""
        if not self._uses_dashscope_native_search():
            return False
        from aero.data.web_search import bailian_native_search_uses_multimodal_generation

        return bailian_native_search_uses_multimodal_generation(self.config.model)

    def _dashscope_native_endpoint(self) -> str:
        if self._uses_dashscope_multimodal_search():
            return self.config.dashscope_multimodal_generation_endpoint
        return self.config.dashscope_generation_endpoint

    async def _send(
        self, messages: list[Message], tools: list[dict] | None
    ) -> dict:
        self.last_search_references = []
        self.last_search_performed = False
        headers = await self._request_headers()
        self._identify_request(headers)
        body = self._request_body(messages, tools=tools, stream=False)
        endpoint = self.config.endpoint

        logger.info("llm.request", model=self.config.model, tool_count=len(tools or []))

        for attempt in range(_TRANSIENT_RETRIES + 1):
            try:
                self.last_request_started_at = datetime.now(timezone.utc)
                resp = await self._client.post(endpoint, json=body, headers=headers)
                if resp.status_code == 401 and self.config.provider == "official":
                    if attempt < _TRANSIENT_RETRIES:
                        headers.update(await self._request_headers(force_refresh=True))
                        continue
                    raise RuntimeError(
                        "Aerolytica 官方账户登录已失效，请使用 /login 重新登录。"
                    )
                _raise_for_status(resp)
                return resp.json()
            except _TRANSIENT_HTTP_ERRORS as e:
                if self.config.provider == "official":
                    await self._official_transport_failure(headers)
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
        if self.config.max_tokens is not None:
            body["max_tokens"] = self.config.max_tokens
        if stream:
            body["stream_options"] = {"include_usage": True}
        if self.config.reasoning_effort:
            body["reasoning_effort"] = self.config.reasoning_effort
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.config.provider == "official" and self.relay_turn_id:
            body["relay_turn_id"] = self.relay_turn_id
        return body

    def _dashscope_request_body(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        *,
        stream: bool = False,
    ) -> dict:
        """Reject the removed native Generation/search protocol."""
        del messages, tools, stream
        raise RuntimeError(
            "DashScope 原生 Generation/内置联网已停用；"
            "百炼聊天必须使用 OpenAI-compatible API，联网必须调用 search_web。"
        )

    async def _dashscope_stream(
        self, messages: list[Message], tools: list[dict] | None
    ) -> AsyncGenerator[StreamEvent, None]:
        """Normalize native DashScope SSE into the application's stream API."""
        body = self._dashscope_request_body(messages, tools=tools, stream=True)
        headers = self._dashscope_headers(stream=True)
        logger.info(
            "llm.dashscope_native_stream",
            model=self.config.model,
            tool_count=len(tools or []),
            endpoint=self._dashscope_native_endpoint(),
            multimodal=self._uses_dashscope_multimodal_search(),
        )

        for attempt in range(_TRANSIENT_RETRIES + 1):
            self.last_search_references = []
            self.last_search_performed = False
            emitted = False
            captured_usage: dict | None = None
            content_so_far = ""
            tool_calls: dict[int, dict] = {}
            sent_references: set[str] = set()
            saw_model_output = False
            raw_response_lines: list[str] = []

            def process_payload(payload: object) -> tuple[list[str], str]:
                """Consume one native SSE packet or a non-SSE JSON response."""
                nonlocal captured_usage, content_so_far, saw_model_output
                if not isinstance(payload, dict):
                    return [], ""

                _raise_dashscope_payload_error(payload)
                output = payload.get("output")
                if not isinstance(output, dict):
                    return [], ""
                saw_model_output = True

                usage = payload.get("usage") or output.get("usage")
                if isinstance(usage, dict):
                    captured_usage = usage

                urls = _dashscope_reference_urls(payload)
                if _dashscope_search_performed(payload):
                    self.last_search_performed = True
                new_urls = [url for url in urls if url not in sent_references]
                if new_urls:
                    sent_references.update(new_urls)

                message = _dashscope_message(payload)
                content = message.get("content") if isinstance(message, dict) else ""
                if isinstance(content, list):
                    content = "".join(
                        item.get("text", "") if isinstance(item, dict) else str(item)
                        for item in content
                    )

                delta = ""
                if isinstance(content, str) and content:
                    # Native SSE may send increments or accumulated text,
                    # depending on the model/version.  Normalize both.
                    if content.startswith(content_so_far):
                        delta = content[len(content_so_far) :]
                        content_so_far = content
                    elif content != content_so_far:
                        delta = content
                        content_so_far += content

                for index, raw_call in enumerate(
                    message.get("tool_calls", []) if isinstance(message, dict) else []
                ):
                    _merge_dashscope_tool_call(tool_calls, index, raw_call)
                return new_urls, delta

            try:
                self.last_request_started_at = datetime.now(timezone.utc)
                async with self._client.stream(
                    "POST",
                    self._dashscope_native_endpoint(),
                    json=body,
                    headers=headers,
                ) as response:
                    await _raise_for_status_stream(response)
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("data:"):
                            raw = line.split(":", 1)[1].strip()
                            if raw == "[DONE]":
                                break
                            try:
                                data = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                        elif line.startswith(("id:", "event:", "retry:", ":")):
                            continue
                        else:
                            # A native request can fail with HTTP 200 and a
                            # regular JSON error body instead of SSE.  Keep it
                            # so it can become a visible diagnostic below,
                            # rather than silently completing with no answer.
                            raw_response_lines.append(line)
                            continue

                        new_urls, delta = process_payload(data)
                        if new_urls:
                            yield StreamEvent(type="references", references=new_urls)
                        if delta:
                            emitted = True
                            yield StreamEvent(type="text", content=delta)

                    if raw_response_lines:
                        try:
                            raw_payload = json.loads("\n".join(raw_response_lines))
                        except json.JSONDecodeError as e:
                            raise RuntimeError(
                                "阿里云百炼联网搜索返回了无法解析的响应，请稍后重试。"
                            ) from e
                        new_urls, delta = process_payload(raw_payload)
                        if new_urls:
                            yield StreamEvent(type="references", references=new_urls)
                        if delta:
                            emitted = True
                            yield StreamEvent(type="text", content=delta)

                    # Some DashScope gateways close a completed SSE response
                    # without a literal ``[DONE]`` marker.  Treat a clean EOF
                    # as completion, but never present an empty response as a
                    # successful answer.
                    self.last_usage = captured_usage
                    self.last_search_references = list(sent_references)
                    completed_calls: list[ToolCall] = []
                    for index in sorted(tool_calls):
                        call = _dashscope_tool_call(tool_calls[index], index)
                        if call is not None:
                            completed_calls.append(call)
                    if not emitted and not completed_calls:
                        if saw_model_output:
                            raise RuntimeError(
                                "阿里云百炼联网搜索未返回生成内容，请稍后重试；"
                                "如果持续出现，请检查模型是否支持联网搜索和账户状态。"
                            )
                        raise RuntimeError(
                            "阿里云百炼联网搜索未返回有效响应，请稍后重试；"
                            "如果持续出现，请检查模型是否支持联网搜索和账户状态。"
                        )
                    for call in completed_calls:
                        emitted = True
                        yield StreamEvent(type="tool_call", tool_call=call)
                    yield StreamEvent(type="done", usage=captured_usage)
                    return
            except _TRANSIENT_HTTP_ERRORS as e:
                if emitted:
                    raise _stream_interrupted_error(e) from e
                if attempt < _TRANSIENT_RETRIES:
                    logger.warning(
                        "llm.dashscope_native_stream.retry",
                        error=repr(e),
                        attempt=attempt + 1,
                    )
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                raise _connection_error(e) from e

    @staticmethod
    def _format_msg(m: Message) -> dict:
        msg: dict = {"role": m.role, "content": m.content}
        if m.role == "tool" or m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id or ""
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

    @classmethod
    def _format_dashscope_message(cls, m: Message, *, multimodal: bool) -> dict:
        """Format a message for the selected native DashScope protocol."""
        msg = cls._format_msg(m)
        if multimodal and isinstance(msg.get("content"), str):
            # The multimodal-generation endpoint requires each content part to
            # be typed, even for a text-only user turn.
            msg["content"] = [{"text": msg["content"]}]
        return msg


def _dashscope_message(response: dict) -> dict:
    """Read a native Generation message without assuming an SSE shape."""
    output = response.get("output")
    if not isinstance(output, dict):
        return {}
    choices = output.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        return message if isinstance(message, dict) else {}
    message = output.get("message")
    return message if isinstance(message, dict) else {}


def _raise_dashscope_payload_error(response: object) -> None:
    """Turn DashScope's HTTP-200 error payloads into visible failures.

    DashScope can return a structured error in a successful HTTP response.  If
    it is ignored, the stream ends with no text and the chat UI appears stuck.
    A normal response contains ``output``; anything else with an error code or
    message is actionable and must be surfaced immediately.
    """
    if not isinstance(response, dict) or isinstance(response.get("output"), dict):
        return

    error = response.get("error")
    code = response.get("code")
    message = response.get("message")
    if isinstance(error, dict):
        code = code or error.get("code") or error.get("type")
        message = message or error.get("message")
    elif isinstance(error, str):
        message = message or error

    if not code and not message:
        return
    request_id = response.get("request_id") or response.get("requestId")
    suffix = f"（请求 ID：{request_id}）" if request_id else ""
    detail = str(message or code).strip()[:500]
    raise RuntimeError(f"阿里云百炼联网搜索请求失败：{detail}{suffix}")


def _dashscope_to_openai_response(response: dict) -> dict:
    """Adapt native Generation output to the established client contract."""
    output = response.get("output")
    output_dict = output if isinstance(output, dict) else {}
    usage = response.get("usage") or output_dict.get("usage")
    return {"choices": [{"message": _dashscope_message(response)}], "usage": usage}


def _dashscope_reference_urls(response: dict) -> list[str]:
    """Extract source URLs returned by DashScope native web search."""
    output = response.get("output")
    if not isinstance(output, dict):
        return []
    info = output.get("search_info")
    if not isinstance(info, dict):
        return []
    results = info.get("search_results")
    if not isinstance(results, list):
        return []
    urls: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        url = result.get("url") or result.get("source_url")
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            urls.append(url)
    return list(dict.fromkeys(urls))


def _dashscope_search_performed(response: dict) -> bool:
    """Detect provider evidence that native search ran for this request."""
    output = response.get("output")
    return isinstance(output, dict) and isinstance(output.get("search_info"), dict)


def _merge_dashscope_tool_call(
    buffer: dict[int, dict], index: int, raw_call: object
) -> None:
    """Merge native streaming tool call chunks defensively."""
    if not isinstance(raw_call, dict):
        return
    current = buffer.setdefault(
        index,
        {"id": "", "function": {"name": "", "arguments": ""}},
    )
    if raw_call.get("id"):
        current["id"] = raw_call["id"]
    function = raw_call.get("function")
    if not isinstance(function, dict):
        function = raw_call
    name = function.get("name")
    arguments = function.get("arguments")
    if isinstance(name, str):
        current_name = current["function"]["name"]
        current["function"]["name"] = (
            name if name.startswith(current_name) else current_name + name
        )
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments, ensure_ascii=False)
    if isinstance(arguments, str):
        current_args = current["function"]["arguments"]
        current["function"]["arguments"] = (
            arguments if arguments.startswith(current_args) else current_args + arguments
        )


def _dashscope_tool_call(raw_call: dict, index: int) -> ToolCall | None:
    function = raw_call.get("function")
    if not isinstance(function, dict) or not function.get("name"):
        return None
    return ToolCall(
        id=str(raw_call.get("id") or f"call_{index}"),
        name=str(function["name"]),
        arguments=_parse_args(function.get("arguments", "")),
    )


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
        body = e.response.text
        lowered_body = body.lower()
        request_id = _request_id_from_error_body(body)
        if status == 402 or any(
            marker in lowered_body
            for marker in (
                "insufficient_balance",
                "insufficient balance",
                "insufficient_quota",
                "quota exceeded",
                "quota_exceeded",
                "余额不足",
                "arrearage",
            )
        ):
            provider = _provider_name_from_response(e.response)
            suffix = f" 请求 ID：{request_id}。" if request_id else ""
            if "aerolytica.skyviewor.team" in (e.response.request.url.host or ""):
                raise RuntimeError(
                    "Aerolytica 官方账户余额不足或当前套餐额度已用尽。"
                    f"请使用 /account 查看可用额度。{suffix}"
                ) from e
            raise RuntimeError(
                f"{provider}账户余额不足或当前套餐额度已用尽，无法调用模型。"
                f"请登录{provider}开放平台充值或检查可用额度后重试。{suffix}"
            ) from e
        if status == 401:
            raise RuntimeError(
                "LLM API 未授权（401）：当前模型服务商的 API key 无效或不匹配。"
            ) from e
        if status == 429:
            raise RuntimeError(_rate_limit_error_message(e.response, body, request_id)) from e
        if status == 400:
            if "Content Exists Risk" in body:
                raise RuntimeError(
                    "当前对话内容被模型服务商的安全策略拦截（Content Exists Risk）。\n"
                    "建议换一种表述方式重试，或使用 /provider 切换到其他服务商"
                    "（如阿里云百炼、Kimi）。"
                ) from e
            if "content_filter" in body.lower() or "safety" in body.lower():
                raise RuntimeError(
                    "当前对话内容触发了模型服务商的内容过滤策略。请尝试换一种说法，"
                    "或用 /provider 切换到其他服务商。"
                ) from e
        if 500 <= status < 600:
            raise RuntimeError(f"LLM 服务暂时不可用（{status}），请稍后再试。") from e
        raise RuntimeError(f"LLM API 请求失败（HTTP {status}）：{body}") from e


def _provider_name_from_response(response: httpx.Response) -> str:
    host = (response.request.url.host or "").lower()
    if "aliyuncs" in host:
        return "阿里云百炼 "
    if "deepseek" in host:
        return "DeepSeek "
    if "moonshot" in host or "kimi" in host:
        return "Kimi "
    return "模型服务"


def _request_id_from_error_body(body: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("request_id", "requestId", "RequestId"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _rate_limit_error_message(response: httpx.Response, body: str, request_id: str) -> str:
    """Explain HTTP 429 from the provider without guessing its billing state."""
    provider = _provider_name_from_response(response)
    detail = _error_message_from_body(body).lower()
    suffix_parts = []
    retry_after = response.headers.get("retry-after")
    if retry_after:
        suffix_parts.append(f"建议等待 {retry_after} 秒后重试")
    if request_id:
        suffix_parts.append(f"请求 ID：{request_id}")
    suffix = f"（{'；'.join(suffix_parts)}）" if suffix_parts else ""

    if any(
        marker in detail
        for marker in ("rate limit", "rate_limit", "too many requests", "并发", "频率")
    ):
        return f"{provider}当前触发请求频率或并发限制（429）。请稍后重试。{suffix}"
    return (
        f"{provider}拒绝了本次请求（HTTP 429），但接口未说明具体原因。"
        "这通常与请求频率、并发数或账户可用额度有关；请检查开放平台的余额/套餐额度和限流状态后重试。"
        f"{suffix}"
    )


def _error_message_from_body(body: str) -> str:
    """Read common OpenAI-compatible error message locations safely."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body
    if not isinstance(payload, dict):
        return body
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "type", "code"):
            value = error.get(key)
            if value:
                return str(value)
    for key in ("message", "type", "code"):
        value = payload.get(key)
        if value:
            return str(value)
    return body


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
