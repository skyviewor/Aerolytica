"""Tests for the MCP channel managed by the official Relay."""

import json

import httpx
import pytest

from aero.core.official_mcp import OfficialMcpClient


class StubAccount:
    def __init__(self):
        self.requests: list[bool] = []

    async def access_token(self, *, force_refresh=False):
        self.requests.append(force_refresh)
        return "jwt-refreshed" if force_refresh else "jwt-initial"

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_prepare_and_call_use_relay_owned_mcp_with_jwt():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer jwt-initial"
        if request.url.path.endswith("/mcp/tools"):
            return httpx.Response(
                200,
                json={
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "description": "Search the web",
                                "parameters": {"type": "object"},
                            },
                        }
                    ]
                },
                request=request,
            )
        body = json.loads(request.content)
        assert body["category"] == "web_search"
        assert body["turn_id"] == "turn-test"
        assert body["call_id"] == "call-test"
        return httpx.Response(
            200,
            json={"name": "web_search", "content": "最新搜索结果", "is_error": False},
            request=request,
        )

    account = StubAccount()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OfficialMcpClient(
        account=account,
        client=http_client,
        base_url="https://relay.test/v1",
    )

    tools = await client.list_tools()
    result = await client.call(
        "web_search", {"query": "今天台风"}, turn_id="turn-test", call_id="call-test"
    )

    assert tools[0]["function"]["name"] == "web_search"
    assert result["content"] == "最新搜索结果"
    assert seen == [
        ("GET", "/v1/mcp/tools"),
        ("POST", "/v1/mcp/call"),
    ]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_mcp_request_refreshes_jwt_once_after_401():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer jwt-initial":
            return httpx.Response(401, json={"error": {"message": "expired"}}, request=request)
        return httpx.Response(
            200,
            json={"tools": []},
            request=request,
        )

    account = StubAccount()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OfficialMcpClient(
        account=account,
        client=http_client,
        base_url="https://relay.test/v1",
    )

    tools = await client.list_tools()

    assert tools == []
    assert account.requests == [False, True]
    await http_client.aclose()
