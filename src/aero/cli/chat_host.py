"""Explicit foreground-to-resident handoff preserving the conversation."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aero.core.remote_agent import RemoteAgentError, RemoteAgentRegistry


class HostLaunchError(RemoteAgentError):
    """Cloud handoff committed; the foreground must not resume execution."""


def parse_host_command(text: str) -> str:
    parts = text.split()
    if len(parts) == 2 and parts[0] == "/host":
        return parts[1]
    if len(parts) == 3 and parts[:2] == ["/agent", "background"]:
        return parts[2]
    raise ValueError(
        "用法：/agent background my_agent；别名：/host my_agent（保留当前对话，转为后台常驻）"
    )


async def host_chat(presence, name: str, project_dir: Path, session_id: str) -> dict:
    from aero.cli.agent_background import launch_background_agent

    registry = RemoteAgentRegistry()
    name = registry.ensure_name_available(name)
    if not presence or not presence.session.data.is_logged_in:
        raise RemoteAgentError("请先在聊天中执行 /login，登录后再托管。")
    if presence._active_command_id or not presence.is_idle():
        raise RemoteAgentError("当前会话有任务正在执行，请完成或取消后再托管。")
    if not presence.agent_id:
        response = await presence.session.request(
            "POST",
            "/v1/agents/chats",
            json={"session_id": session_id, "title": presence.title()},
        )
        if response.status_code >= 400:
            raise RemoteAgentError("当前聊天登记失败，请检查登录和在线名额后重试。")
        created = response.json()
        presence.agent_id, presence.token = created["agent_id"], created["token"]
        presence._active_session_id = session_id
    if presence._active_session_id != session_id:
        raise RemoteAgentError("云端会话尚未同步到当前对话，请稍后重试。")
    response = await presence.session.request(
        "POST",
        f"/v1/agents/{presence.agent_id}/chat-host",
        json={"name": name, "presence_token": presence.token},
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        raise RemoteAgentError(str(detail or "转为常驻智能体失败，请稍后重试。"))
    result = response.json()
    registry.add(
        agent_id=result["agent_id"],
        name=result["name"],
        description="由 aero chat 当前会话转为常驻",
        token=result["token"],
    )
    await presence.close()
    try:
        pid, log = await asyncio.to_thread(
            launch_background_agent,
            result["agent_id"],
            project_dir,
        )
    except Exception as exc:
        raise HostLaunchError(
            f"会话已转为常驻，但后台启动失败：{exc}。"
            f"历史和注册已保留，请在原目录执行 aero agent run {name} --background。"
        ) from exc
    return {**result, "pid": pid, "log": log}
