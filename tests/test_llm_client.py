"""Tests for LLM client (with mocked HTTP)."""

import httpx
import pytest

from meteora.agent.llm_client import (
    LLMClient,
    LLMConfig,
    _find_tool_call_marker_start,
    _parse_args,
    _parse_content_tool_calls,
    _raise_for_status_stream,
    _safe_content_stream_end,
)
from meteora.core.types import Message


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
        "my-meteora-test/data/era5_2m_temperature_sfc_202505.nc"
        "</｜ | DSML | | parameter> </｜ | DSML | | invoke> "
        "</｜ | DSML | | tool_calls>"
    )

    clean_text, calls = _parse_content_tool_calls(text)

    assert clean_text == "让我看一下数据的详细信息："
    assert len(calls) == 1
    assert calls[0].name == "inspect_nc"
    assert calls[0].arguments == {
        "file_path": "my-meteora-test/data/era5_2m_temperature_sfc_202505.nc",
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


def test_request_body_includes_reasoning_effort():
    client = LLMClient(LLMConfig(api_key="sk-test", reasoning_effort="high"))
    body = client._request_body([Message(role="user", content="Hi")], stream=True)

    assert body["model"] == "deepseek-chat"
    assert body["stream"] is True
    assert body["reasoning_effort"] == "high"


def test_request_body_omits_empty_reasoning_effort():
    client = LLMClient(LLMConfig(api_key="sk-test"))
    body = client._request_body([Message(role="user", content="Hi")])

    assert "reasoning_effort" not in body


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
