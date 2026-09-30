"""Client for MCP tools managed by the Aerolytica official Relay."""

from __future__ import annotations

from typing import Any

import httpx

from aero.core.official_account import (
    OfficialAccountError,
    OfficialAccountSession,
    relay_llm_url,
)


class OfficialMcpError(OfficialAccountError):
    """The official Relay MCP service could not complete an operation."""


def _relay_error(response: httpx.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or fallback)
    return str(payload.get("message") or payload.get("detail") or fallback)


class OfficialMcpClient:
    """Authenticated access to Relay-owned MCP discovery and execution."""

    def __init__(
        self,
        *,
        account: OfficialAccountSession | None = None,
        client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
    ) -> None:
        self.account = account or OfficialAccountSession()
        self._owns_account = account is None
        self._client = client or httpx.AsyncClient(timeout=90)
        self._owns_client = client is None
        self.base_url = (base_url or relay_llm_url()).rstrip("/")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        if self._owns_account:
            await self.account.close()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(2):
            token = await self.account.access_token(force_refresh=attempt == 1)
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {token}"
            headers["Content-Type"] = "application/json"
            try:
                response = await self._client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise OfficialMcpError("无法连接 Aerolytica 官方 MCP 服务。") from exc
            if response.status_code != 401 or attempt == 1:
                return response
        raise OfficialMcpError("Aerolytica 官方账户登录已失效。")

    async def list_tools(self) -> list[dict[str, Any]]:
        """Load Relay-managed tools and let the model decide whether to call them."""
        response = await self._request("GET", "/mcp/tools")
        if response.status_code >= 400:
            raise OfficialMcpError(_relay_error(response, "无法获取 Aerolytica 官方 MCP 工具。"))
        payload = response.json()
        tools = payload.get("tools") if isinstance(payload, dict) else None
        if not isinstance(tools, list):
            raise OfficialMcpError("Aerolytica 官方 MCP 返回了无效的工具列表。")
        return [tool for tool in tools if isinstance(tool, dict)]

    async def call(
        self, name: str, arguments: dict[str, Any], *, turn_id: str = "", call_id: str = ""
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/mcp/call",
            json={
                "category": "web_search",
                "name": name,
                "arguments": arguments,
                "turn_id": turn_id,
                "call_id": call_id,
            },
        )
        if response.status_code >= 400:
            raise OfficialMcpError(
                _relay_error(response, f"Aerolytica 官方 MCP 工具 {name} 调用失败。")
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise OfficialMcpError("Aerolytica 官方 MCP 返回了无效响应。")
        return payload
