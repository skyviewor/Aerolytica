"""Account policy snapshot. Custom prompts are injected only by Relay."""

from __future__ import annotations

from dataclasses import dataclass

from aero.core.official_account import OfficialAccountError, OfficialAccountSession


@dataclass(frozen=True)
class AgentPreferences:
    max_rounds: int | None = 999
    version: int = 0


async def fetch_agent_preferences() -> AgentPreferences:
    session = OfficialAccountSession()
    try:
        response = await session.request("GET", "/v1/agents/preferences")
        response.raise_for_status()
        payload = response.json()
        limit = payload.get("max_rounds", 999)
        version = payload.get("version", 0)
        if limit is not None and (type(limit) is not int or limit < 1):
            raise OfficialAccountError("账户轮次设置无效")
        if type(version) is not int or version < 0:
            raise OfficialAccountError("账户设置版本无效")
        return AgentPreferences(limit, version)
    finally:
        await session.close()


async def begin_agent_question(turn_id: str) -> AgentPreferences:
    """Atomically freeze local question policy before compaction or inference."""
    session = OfficialAccountSession()
    try:
        response = await session.request(
            "POST", "/v1/agents/preferences/questions", json={"turn_id": turn_id}
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("turn_id") != turn_id:
            raise OfficialAccountError("账户问答标识无效")
        limit = payload.get("max_rounds", 999)
        version = payload.get("version", 1)
        if limit is not None and (type(limit) is not int or limit < 1):
            raise OfficialAccountError("账户轮次设置无效")
        if type(version) is not int or version < 1:
            raise OfficialAccountError("账户设置版本无效")
        return AgentPreferences(limit, version)
    finally:
        await session.close()
