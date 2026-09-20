"""Tests for LLM client (with mocked HTTP)."""

import httpx
import pytest

from aero.agent.llm_client import (
    LLMClient,
    LLMConfig,
    _dashscope_reference_urls,
    _find_tool_call_marker_start,
    _parse_args,
    _parse_content_tool_calls,
    _raise_for_status,
    _raise_for_status_stream,
    _safe_content_stream_end,
)
from aero.core.types import Message


def test_parse_args_string():
    result = _parse_args('{"x": 1, "y": 2}')
    assert result == {"x": 1, "y": 2}


def test_parse_args_dict():
    result = _parse_args({"x": 1})
    assert result == {"x": 1}


def test_parse_args_invalid():
    result = _parse_args("not json")
    assert result == {}


def test_parse_dsml_content_tool_calls():
    text = """我先确认可用变量。
<｜DSML｜tool_calls>
<｜DSML｜invoke name="download_era5">
<｜DSML｜parameter name="variables" string="false">["2m_temperature"]</｜DSML｜parameter>
<｜DSML｜parameter name="year" string="false">2026</｜DSML｜parameter>
<｜DSML｜parameter name="month" string="false">1</｜DSML｜parameter>
<｜DSML｜parameter name="area" string="false">[21,108,18,112]</｜DSML｜parameter>
</｜DSML｜invoke>
</｜DSML｜tool_calls>"""

    clean_text, calls = _parse_content_tool_calls(text)

    assert clean_text == "我先确认可用变量。"
    assert len(calls) == 1
    assert calls[0].name == "download_era5"
    assert calls[0].arguments == {
        "variables": ["2m_temperature"],
        "year": 2026,
        "month": 1,
        "area": [21, 108, 18, 112],
    }


def test_parse_spaced_dsml_content_tool_calls():
    text = "\n".join(
        [
            "我调整一下范围再试：",
            "<｜ | DSML | | tool_calls>",
            '<｜ | DSML | | invoke name="download_era5">',
            '<｜ | DSML | | parameter name="variables" string="false">'
            '["2m_temperature"]</｜ | DSML | | parameter>',
            '<｜ | DSML | | parameter name="year" string="false">'
            "2026</｜ | DSML | | parameter>",
            '<｜ | DSML | | parameter name="month" string="false">'
            "1</｜ | DSML | | parameter>",
            '<｜ | DSML | | parameter name="area" string="false">'
            "[20.5,110.0,19.5,110.8]</｜ | DSML | | parameter>",
            "</｜ | DSML | | invoke>",
            "</｜ | DSML | | tool_calls>",
        ]
    )

    clean_text, calls = _parse_content_tool_calls(text)

    assert clean_text == "我调整一下范围再试："
    assert len(calls) == 1
    assert calls[0].name == "download_era5"
    assert calls[0].arguments == {
        "variables": ["2m_temperature"],
        "year": 2026,
        "month": 1,
        "area": [20.5, 110.0, 19.5, 110.8],
    }


def test_parse_inline_spaced_dsml_content_tool_calls():
    text = (
        "让我看一下数据的详细信息：\n"
        '<｜ | DSML | | tool_calls> <｜ | DSML | | invoke name="inspect_nc"> '
        '<｜ | DSML | | parameter name="file_path" string="false">'
        "my-aero-test/data/era5_2m_temperature_sfc_202505.nc"
        "</｜ | DSML | | parameter> </｜ | DSML | | invoke> "
        "</｜ | DSML | | tool_calls>"
    )

    clean_text, calls = _parse_content_tool_calls(text)

    assert clean_text == "让我看一下数据的详细信息："
    assert len(calls) == 1
    assert calls[0].name == "inspect_nc"
    assert calls[0].arguments == {
        "file_path": "my-aero-test/data/era5_2m_temperature_sfc_202505.nc",
    }


def test_parse_deepseek_content_tool_calls():
    text = """好的。
<｜tool_calls｜><｜tool_call_begin｜>function<｜tool_sep｜>download_era5
```json
{"variables":["2m_temperature"],"year":2026,"month":1}
```<｜tool_call_end｜><｜tool_calls_end｜>"""

    clean_text, calls = _parse_content_tool_calls(text)

    assert clean_text == "好的。"
    assert len(calls) == 1
    assert calls[0].name == "download_era5"
    assert calls[0].arguments == {
        "variables": ["2m_temperature"],
        "year": 2026,
        "month": 1,
    }


def test_find_tool_call_marker_start():
    text = "先下载数据。\n<｜DSML｜tool_calls><｜DSML｜invoke name=\"download_era5\">"

    assert _find_tool_call_marker_start(text) == len("先下载数据。\n")
    assert _find_tool_call_marker_start("普通回复，没有工具调用。") is None


def test_safe_content_stream_end_only_holds_possible_marker():
    assert _safe_content_stream_end("普通流式回复", 0) == len("普通流式回复")
    assert _safe_content_stream_end("先说一句\n<｜DSML｜tool", 0) == len("先说一句\n")
    assert _safe_content_stream_end("正常比较 1 < 2 后继续说明很多内容", 0) == len(
        "正常比较 1 < 2 后继续说明很多内容"
    )


def test_llm_config_endpoint():
    cfg = LLMConfig(provider="deepseek")
    assert "api.deepseek.com" in cfg.endpoint

    cfg = LLMConfig(provider="openai")
    assert "api.openai.com" in cfg.endpoint

    cfg = LLMConfig(provider="ollama")
    assert "localhost:11434" in cfg.endpoint

    cfg = LLMConfig(base_url="http://custom.com")
    assert cfg.endpoint == "http://custom.com/v1/chat/completions"

    cfg = LLMConfig(base_url="https://api.moonshot.cn/v1")
    assert cfg.endpoint == "https://api.moonshot.cn/v1/chat/completions"

    cfg = LLMConfig(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert (
        cfg.endpoint
        == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
    assert (
        cfg.dashscope_generation_endpoint
        == "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
    )
    assert (
        cfg.dashscope_multimodal_generation_endpoint
        == "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
        "multimodal-generation/generation"
    )


def test_request_body_includes_reasoning_effort():
    client = LLMClient(LLMConfig(api_key="sk-test", reasoning_effort="high"))
    body = client._request_body([Message(role="user", content="Hi")], stream=True)

    assert body["model"] == "deepseek-v4-flash"
    assert body["stream"] is True
    assert body["reasoning_effort"] == "high"


def test_request_body_omits_empty_reasoning_effort():
    client = LLMClient(LLMConfig(api_key="sk-test"))
    body = client._request_body([Message(role="user", content="Hi")])

    assert "reasoning_effort" not in body


def test_request_body_can_limit_completion_tokens_for_connectivity_check():
    client = LLMClient(LLMConfig(api_key="sk-test", max_tokens=1))
    body = client._request_body([Message(role="user", content="Ping")])

    assert body["max_tokens"] == 1


def test_official_request_body_includes_stable_relay_turn_id():
    client = LLMClient(LLMConfig(provider="official", model="auto"))
    client.relay_turn_id = "turn-stable"

    body = client._request_body([Message(role="user", content="Hi")])

    assert body["relay_turn_id"] == "turn-stable"


@pytest.mark.asyncio
async def test_official_relay_uses_jwt_and_refreshes_once_after_401(monkeypatch):
    token_requests: list[bool] = []

    class StubOfficialSession:
        async def access_token(self, *, force_refresh=False):
            token_requests.append(force_refresh)
            return "jwt-refreshed" if force_refresh else "jwt-initial"

        async def close(self):
            return None

    monkeypatch.setattr(
        "aero.core.official_account.OfficialAccountSession",
        StubOfficialSession,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer jwt-initial":
            return httpx.Response(401, json={"detail": "expired"}, request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=request,
        )

    client = LLMClient(
        LLMConfig(
            provider="official",
            model="auto",
            base_url="https://llm.aerolytica.skyviewor.team/v1",
        )
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    result = await client.chat([Message(role="user", content="Hi")])

    assert result == "ok"
    assert token_requests == [False, True]
    await client.close()


def test_official_relay_402_points_to_account_command():
    request = httpx.Request(
        "POST",
        "https://llm.aerolytica.skyviewor.team/v1/chat/completions",
    )
    response = httpx.Response(
        402,
        json={"error": {"code": "insufficient_quota"}},
        request=request,
    )

    with pytest.raises(RuntimeError, match="/account"):
        _raise_for_status(response)


def obsolete_dashscope_multimodal_native_search_body_is_used_for_qwen37_flash():
    client = LLMClient(
        LLMConfig(
            provider="bailian",
            model="qwen3.7-flash",
            api_key="sk-test",
            web_search_enabled=True,
        )
    )

    assert client._uses_dashscope_native_search() is True
    assert client._uses_dashscope_multimodal_search() is True
    assert (
        client._dashscope_native_endpoint()
        == client.config.dashscope_multimodal_generation_endpoint
    )

    body = client._dashscope_request_body(
        [Message(role="user", content="最新天气")], stream=True
    )

    assert body["parameters"]["enable_search"] is True
    assert body["parameters"]["search_options"] == {
        "search_strategy": "agent",
        "enable_source": True,
    }
    assert body["parameters"]["incremental_output"] is True
    assert body["input"]["messages"][0]["content"] == [{"text": "最新天气"}]


def obsolete_dashscope_multimodal_native_search_body_is_used_for_qwen35_plus():
    client = LLMClient(
        LLMConfig(
            provider="bailian",
            model="qwen3.5-plus",
            api_key="sk-test",
            web_search_enabled=True,
        )
    )

    assert client._uses_dashscope_native_search() is True
    assert client._uses_dashscope_multimodal_search() is True
    assert (
        client._dashscope_native_endpoint()
        == client.config.dashscope_multimodal_generation_endpoint
    )

    body = client._dashscope_request_body(
        [Message(role="user", content="最新天气")], stream=True
    )

    assert body["parameters"]["search_options"] == {
        "search_strategy": "agent",
        "enable_source": True,
    }
    assert body["input"]["messages"][0]["content"] == [{"text": "最新天气"}]


def obsolete_dashscope_text_native_search_stays_on_text_generation_protocol():
    client = LLMClient(
        LLMConfig(
            provider="bailian",
            model="deepseek-v4-flash",
            api_key="sk-test",
            web_search_enabled=True,
        )
    )

    assert client._uses_dashscope_native_search() is True
    assert client._uses_dashscope_multimodal_search() is False
    assert client._dashscope_native_endpoint() == client.config.dashscope_generation_endpoint

    body = client._dashscope_request_body([Message(role="user", content="最新天气")])
    assert body["parameters"]["enable_search"] is True
    assert body["parameters"]["search_options"] == {
        "search_strategy": "turbo",
        "enable_source": True,
        "enable_citation": True,
        "citation_format": "[ref_<number>]",
    }
    assert body["input"]["messages"][0]["content"] == "最新天气"


def obsolete_dashscope_native_search_keeps_tools_in_parameters():
    client = LLMClient(
        LLMConfig(
            provider="bailian", model="qwen3.5-plus", api_key="sk-test", web_search_enabled=True
        )
    )
    tools = [{"type": "function", "function": {"name": "weather", "parameters": {}}}]

    body = client._dashscope_request_body(
        [Message(role="user", content="天气")], tools=tools, stream=True
    )

    assert body["parameters"]["tools"] == tools
    assert body["parameters"]["tool_choice"] == "auto"
    assert body["parameters"]["search_options"]["search_strategy"] == "agent"
    assert body["input"]["messages"][0]["content"] == [{"text": "天气"}]


def obsolete_dashscope_reference_urls_extracts_provider_sources():
    response = {
        "output": {
            "search_info": {
                "search_results": [
                    {"title": "one", "url": "https://example.com/one"},
                    {"title": "duplicate", "url": "https://example.com/one"},
                    {"title": "invalid", "url": "not-a-url"},
                ]
            }
        }
    }

    assert _dashscope_reference_urls(response) == ["https://example.com/one"]


def obsolete_dashscope_search_info_marks_native_search_as_performed():
    from aero.agent.llm_client import _dashscope_search_performed

    assert _dashscope_search_performed({"output": {"search_info": {"search_results": []}}})
    assert not _dashscope_search_performed({"output": {"text": "无需搜索"}})


def test_llm_request_does_not_accept_brotli_response():
    client = LLMClient(LLMConfig(api_key="sk-test"))

    assert client._headers()["Accept-Encoding"] == "identity"


def test_llm_rate_limit_error_describes_rate_limit_and_retry_time():
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://api.moonshot.cn/v1/chat/completions"),
        headers={"retry-after": "15"},
        json={"error": {"message": "rate limit exceeded"}, "request_id": "req-123"},
    )

    with pytest.raises(RuntimeError, match="请求频率或并发限制") as exc_info:
        _raise_for_status(response)

    assert "等待 15 秒" in str(exc_info.value)
    assert "req-123" in str(exc_info.value)


def test_llm_unknown_429_does_not_claim_balance_is_definitely_insufficient():
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://api.moonshot.cn/v1/chat/completions"),
        json={"error": {"message": "request rejected"}},
    )

    with pytest.raises(RuntimeError, match="未说明具体原因"):
        _raise_for_status(response)


@pytest.mark.asyncio
async def test_stream_error_body_is_read_before_formatting():
    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    response = httpx.Response(
        400,
        request=request,
        stream=httpx.ByteStream(b'{"error":{"message":"bad model"}}'),
    )

    with pytest.raises(RuntimeError) as exc_info:
        await _raise_for_status_stream(response)

    assert "HTTP 400" in str(exc_info.value)
    assert "bad model" in str(exc_info.value)


@pytest.mark.asyncio
async def test_llm_chat_mocked():
    """Mock the LLM API response to test the client."""
    from unittest.mock import AsyncMock, patch

    config = LLMConfig(api_key="sk-test")
    client = LLMClient(config)

    mock_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Hello! How can I help?",
                }
            }
        ]
    }

    with patch.object(client._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json = lambda: mock_response

        result = await client.chat([Message(role="user", content="Hi")])
        assert "Hello" in result

    await client.close()


@pytest.mark.asyncio
async def test_bailian_chat_always_uses_openai_compatible_api_without_native_search():
    from unittest.mock import AsyncMock, patch

    config = LLMConfig(
        provider="bailian",
        model="qwen3.7-flash",
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    client = LLMClient(config)
    response = {"choices": [{"message": {"content": "请调用 search_web"}}]}

    with patch.object(client._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json = lambda: response
        await client.chat([Message(role="user", content="最新台风")])

    assert mock_post.call_args.args[0] == config.endpoint
    body = mock_post.call_args.kwargs["json"]
    assert body["messages"][0]["content"] == "最新台风"
    assert "enable_search" not in body
    assert "search_options" not in body
    assert "input" not in body
    assert client._uses_dashscope_native_search() is False
    await client.close()


@pytest.mark.asyncio
async def test_llm_request_retries_transient_disconnect():
    from unittest.mock import patch

    config = LLMConfig(api_key="sk-test")
    client = LLMClient(config)
    calls = 0
    request = httpx.Request("POST", config.endpoint)

    async def fake_post(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "恢复了"}}]},
        )

    with patch.object(client._client, "post", side_effect=fake_post):
        result = await client.chat([Message(role="user", content="Hi")])

    assert calls == 2
    assert result == "恢复了"
    await client.close()


@pytest.mark.asyncio
async def test_llm_stream_retries_before_any_content():
    from unittest.mock import patch

    class FakeStreamResponse:
        is_error = False
        status_code = 200

        async def aread(self):
            return b""

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"恢复了"}}]}'
            yield "data: [DONE]"

    class FakeStream:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    config = LLMConfig(api_key="sk-test")
    client = LLMClient(config)
    calls = 0

    def fake_stream(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return FakeStream()

    with patch.object(client._client, "stream", side_effect=fake_stream):
        events = [
            event
            async for event in client.chat_with_tools_stream(
                [Message(role="user", content="Hi")],
                tools=[],
            )
        ]

    assert calls == 2
    assert [event.type for event in events] == ["text", "done"]
    assert events[0].content == "恢复了"
    await client.close()


@pytest.mark.asyncio
async def obsolete_dashscope_native_stream_returns_text_and_sources():
    from unittest.mock import patch

    class FakeStreamResponse:
        is_error = False
        status_code = 200

        async def aread(self):
            return b""

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            yield "event:result"
            yield (
                'data: {"output":{"choices":[{"message":{"content":"最"}}],'
                '"search_info":{"search_results":[{"url":"https://example.com/source"}]}}}'
            )
            yield 'data: {"output":{"choices":[{"message":{"content":"最新结果"}}]}}'
            yield "data: [DONE]"

    class FakeStream:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    client = LLMClient(
        LLMConfig(
            provider="bailian", model="qwen3.7-flash", api_key="sk-test", web_search_enabled=True
        )
    )
    with patch.object(client._client, "stream", return_value=FakeStream()) as stream:
        events = [
            event
            async for event in client.chat_with_tools_stream(
                [Message(role="user", content="最新天气")], tools=[]
            )
        ]

    assert [event.type for event in events] == ["references", "text", "text", "done"]
    assert events[0].references == ["https://example.com/source"]
    assert "".join(event.content for event in events if event.type == "text") == "最新结果"
    assert (
        stream.call_args.args[1]
        == client.config.dashscope_multimodal_generation_endpoint
    )
    assert stream.call_args.kwargs["headers"]["X-DashScope-SSE"] == "enable"
    body = stream.call_args.kwargs["json"]
    assert body["parameters"]["incremental_output"] is True
    assert body["parameters"]["search_options"]["search_strategy"] == "agent"
    assert body["input"]["messages"][0]["content"] == [{"text": "最新天气"}]
    await client.close()


@pytest.mark.asyncio
async def obsolete_dashscope_native_empty_clean_stream_is_not_silently_completed():
    from unittest.mock import patch

    class FakeStreamResponse:
        is_error = False
        status_code = 200

        async def aread(self):
            return b""

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            yield "data: [DONE]"

    class FakeStream:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    client = LLMClient(
        LLMConfig(
            provider="bailian", model="qwen3.7-flash", api_key="sk-test", web_search_enabled=True
        )
    )
    with patch.object(client._client, "stream", return_value=FakeStream()):
        with pytest.raises(RuntimeError, match="未返回有效响应"):
            _ = [
                event
                async for event in client.chat_with_tools_stream(
                    [Message(role="user", content="最新天气")], tools=[]
                )
            ]
    await client.close()


@pytest.mark.asyncio
async def obsolete_dashscope_native_json_error_body_is_not_silently_completed():
    from unittest.mock import patch

    class FakeStreamResponse:
        is_error = False
        status_code = 200

        async def aread(self):
            return b""

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            yield '{"code":"InvalidParameter","message":"bad search option"}'

    class FakeStream:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    client = LLMClient(
        LLMConfig(
            provider="bailian", model="qwen3.7-flash", api_key="sk-test", web_search_enabled=True
        )
    )
    with patch.object(client._client, "stream", return_value=FakeStream()):
        with pytest.raises(RuntimeError, match="bad search option"):
            _ = [
                event
                async for event in client.chat_stream([Message(role="user", content="天气")])
            ]
    await client.close()


@pytest.mark.asyncio
async def test_llm_stream_ignores_usage_only_chunk_with_empty_choices():
    from unittest.mock import patch

    class FakeStreamResponse:
        is_error = False
        status_code = 200

        async def aread(self):
            return b""

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            yield 'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}'
            yield 'data: {"choices":[{"delta":{"content":"正常回复"}}]}'
            yield "data: [DONE]"

    class FakeStream:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    client = LLMClient(LLMConfig(api_key="sk-test"))
    with patch.object(client._client, "stream", return_value=FakeStream()):
        events = [
            event
            async for event in client.chat_with_tools_stream(
                [Message(role="user", content="Hi")],
                tools=[],
            )
        ]

    assert [event.type for event in events] == ["text", "done"]
    assert events[0].content == "正常回复"
    assert events[-1].usage == {"prompt_tokens": 3, "completion_tokens": 2}
    await client.close()


@pytest.mark.asyncio
async def test_llm_tool_stream_raises_error_delivered_inside_http_200_sse():
    from unittest.mock import patch

    class FakeStreamResponse:
        is_error = False
        status_code = 200

        async def aread(self):
            return b""

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            yield (
                'data: {"error":{"message":"tool_stream is required",'
                '"type":"upstream_error","code":"InvalidParameter"}}'
            )
            yield "data: [DONE]"

    class FakeStream:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    client = LLMClient(LLMConfig(api_key="sk-test"))
    with patch.object(client._client, "stream", return_value=FakeStream()):
        with pytest.raises(RuntimeError, match="InvalidParameter.*tool_stream is required"):
            _ = [
                event
                async for event in client.chat_with_tools_stream(
                    [Message(role="user", content="Hi")],
                    tools=[],
                )
            ]
    await client.close()


@pytest.mark.asyncio
async def test_llm_tool_stream_rejects_empty_success_response():
    from unittest.mock import patch

    class FakeStreamResponse:
        is_error = False
        status_code = 200

        async def aread(self):
            return b""

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            yield 'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":0}}'
            yield "data: [DONE]"

    class FakeStream:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    client = LLMClient(LLMConfig(api_key="sk-test"))
    with patch.object(client._client, "stream", return_value=FakeStream()):
        with pytest.raises(RuntimeError, match="未返回有效正文或工具调用"):
            _ = [
                event
                async for event in client.chat_with_tools_stream(
                    [Message(role="user", content="Hi")],
                    tools=[],
                )
            ]
    await client.close()


@pytest.mark.asyncio
async def test_non_streaming_empty_choices_returns_empty_text():
    from unittest.mock import AsyncMock, patch

    client = LLMClient(LLMConfig(api_key="sk-test"))
    with patch.object(client, "_send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = {"choices": [], "usage": {"prompt_tokens": 1}}
        assert await client.chat([Message(role="user", content="Hi")]) == ""

    await client.close()
