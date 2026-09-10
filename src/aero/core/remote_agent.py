"""HTTP client for a durable Relay remote-Agent command queue."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from aero.core.config import load_user_secrets, save_user_secrets
from aero.core.official_account import OfficialAccountError, OfficialAccountSession


class RemoteAgentError(OfficialAccountError):
    """A remote Agent request failed."""


_AGENT_NAME_RE = re.compile(r"[a-z]+(?:_[a-z]+)*")


@dataclass(frozen=True)
class RegisteredRemoteAgent:
    agent_id: str
    name: str
    description: str
    token: str


class RemoteAgentRegistry:
    """Manage multiple local Agent credentials without exposing their tokens."""

    def __init__(self) -> None:
        self._secrets = load_user_secrets()

    def list(self) -> list[RegisteredRemoteAgent]:
        self._secrets = load_user_secrets()
        raw = self._secrets.get("remote_agents")
        records = raw.get("agents") if isinstance(raw, dict) else {}
        if not isinstance(records, dict):
            return []
        result = []
        for agent_id, value in records.items():
            if not isinstance(value, dict):
                continue
            token = str(value.get("token") or "")
            name = str(value.get("name") or "")
            if agent_id and name and token:
                result.append(
                    RegisteredRemoteAgent(
                        agent_id=str(agent_id),
                        name=name,
                        description=str(value.get("description") or ""),
                        token=token,
                    )
                )
        return result

    def resolve(self, selector: str | None = None) -> RegisteredRemoteAgent:
        agents = self.list()
        if not agents:
            raise RemoteAgentError("请先运行 aero agent register 注册远程 Agent。")
        if selector:
            normalized = selector.strip()
            for agent in agents:
                if agent.agent_id == normalized or agent.name.lower() == normalized.lower():
                    return agent
            available = ", ".join(agent.name for agent in agents)
            raise RemoteAgentError(f"找不到 Agent「{selector}」。可选 Agent：{available}")
        if len(agents) == 1:
            return agents[0]
        available = ", ".join(agent.name for agent in agents)
        raise RemoteAgentError(f"当前有多个 Agent，请在命令后直接指定名称：{available}")

    def ensure_name_available(self, name: str) -> str:
        name = name.strip()
        if not _AGENT_NAME_RE.fullmatch(name):
            raise RemoteAgentError(
                "Agent 名称只能使用小写英文单词，并用下划线连接，例如 ocean_data。"
            )
        if any(agent.name.lower() == name for agent in self.list()):
            raise RemoteAgentError(f"Agent 名称「{name}」已存在，请换一个名称。")
        return name

    def add(
        self,
        *,
        agent_id: str,
        name: str,
        description: str,
        token: str,
    ) -> RegisteredRemoteAgent:
        name = self.ensure_name_available(name)
        agents = self.list()
        record = RegisteredRemoteAgent(agent_id, name, description.strip()[:500], token)
        self._secrets.pop("remote_agent", None)
        self._secrets["remote_agents"] = {
            "version": 1,
            "agents": {
                agent.agent_id: {
                    "name": agent.name,
                    "description": agent.description,
                    "token": agent.token,
                }
                for agent in [*agents, record]
            },
        }
        save_user_secrets(self._secrets)
        return record


class AgentProcessLock:
    """Prevent two local processes from consuming the same Agent queue."""

    def __init__(self, agent_id: str) -> None:
        lock_dir = Path.home() / ".aero" / "agent-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        self.path = lock_dir / f"{agent_id}.lock"
        self._handle = None

    def acquire(self) -> None:
        self._handle = self.path.open("a+")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise RemoteAgentError("该 Agent 已在本机运行，不能重复启动。") from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"{os.getpid()}\n")
        self._handle.flush()

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


class RemoteAgentClient:
    """Register an Agent and exchange commands using long polling plus HTTP POST."""

    def __init__(
        self,
        session: OfficialAccountSession | None = None,
        agent_http: httpx.AsyncClient | None = None,
        *,
        agent_selector: str | None = None,
        select_agent: bool = True,
    ) -> None:
        self.registry = RemoteAgentRegistry()
        selected = self.registry.resolve(agent_selector) if select_agent else None
        self.session = session or OfficialAccountSession()
        self._owns_session = session is None
        self._owns_http = agent_http is None
        self.agent_id = selected.agent_id if selected else ""
        self.agent_token = selected.token if selected else ""
        self.agent_name = selected.name if selected else ""
        self.agent_description = selected.description if selected else ""
        base_url = str(getattr(self.session, "base_url", "") or "").rstrip("/")
        self._agent_http = agent_http or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(35.0, connect=10.0),
        )

    async def close(self) -> None:
        if self._owns_http:
            await self._agent_http.aclose()
        if self._owns_session:
            await self.session.close()

    async def register(
        self,
        name: str,
        *,
        description: str = "",
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        name = self.registry.ensure_name_available(name)
        response = await self.session.request(
            "POST",
            "/v1/agents/register",
            json={
                "name": name,
                "description": description[:500],
                **({"project_id": project_id} if project_id else {}),
                **({"session_id": session_id} if session_id else {}),
            },
        )
        if response.status_code >= 400:
            raise RemoteAgentError(_error(response, "远程 Agent 注册失败。"))
        result = response.json()
        self.agent_id = str(result.get("agent_id") or "")
        self.agent_name = str(result.get("name") or name[:128])
        self.agent_token = str(result.get("token") or "")
        if self.agent_id and self.agent_token:
            record = self.registry.add(
                agent_id=self.agent_id,
                name=self.agent_name,
                description=str(result.get("description") or description),
                token=self.agent_token,
            )
            self.agent_description = record.description
        return result

    async def list_registered_remote_agents(self) -> list[dict[str, Any]]:
        response = await self.session.request("GET", "/v1/agents")
        if response.status_code >= 400:
            raise RemoteAgentError(_error(response, "查询远程 Agent 列表失败。"))
        remote = {
            str(item.get("agent_id")): item
            for item in response.json().get("agents", [])
            if isinstance(item, dict) and item.get("agent_id")
        }
        result = []
        for local in self.registry.list():
            item = remote.get(local.agent_id, {})
            result.append(
                {
                    "name": item.get("name") or local.name,
                    "description": item.get("description") or local.description,
                    "agent_id": local.agent_id,
                    "status": item.get("status") or "unavailable",
                    "last_seen_at": item.get("last_seen_at"),
                }
            )
        return result

    async def status(self) -> dict[str, Any]:
        self._require_registration()
        response = await self.session.request("GET", "/v1/agents")
        if response.status_code >= 400:
            raise RemoteAgentError(_error(response, "查询远程 Agent 状态失败。"))
        agents = response.json().get("agents") or []
        for agent in agents:
            if isinstance(agent, dict) and agent.get("agent_id") == self.agent_id:
                return agent
        raise RemoteAgentError("服务器上不存在当前本地保存的远程 Agent。")

    async def wait_for_command(self, *, wait_seconds: int = 30) -> dict[str, Any] | None:
        self._require_registration()
        response = await self._agent_request(
            "GET",
            f"/v1/agents/{self.agent_id}/commands/next",
            params={"wait_seconds": max(0, min(wait_seconds, 30))},
        )
        if response.status_code == 204:
            return None
        if response.status_code >= 400:
            raise RemoteAgentError(_error(response, "等待远程命令失败。"))
        command = response.json().get("command")
        return command if isinstance(command, dict) else None

    async def update_command_status(
        self,
        command_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        response = await self._agent_request(
            "POST",
            f"/v1/agents/{self.agent_id}/commands/{command_id}/status",
            json={"status": status, "error": error},
        )
        if response.status_code >= 400:
            raise RemoteAgentError(_error(response, "更新远程命令状态失败。"))
        return response.json()

    async def send_event(
        self,
        event_type: str,
        *,
        content: str = "",
        in_reply_to: str | None = None,
        status: str | None = None,
        client_event_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "type": event_type,
            "content": content.strip(),
            "in_reply_to": in_reply_to,
            "status": status,
            "client_event_id": client_event_id or f"evt_{uuid.uuid4().hex}",
        }
        response = await self._agent_request(
            "POST", f"/v1/agents/{self.agent_id}/events", json=payload
        )
        if response.status_code >= 400:
            raise RemoteAgentError(_error(response, "回传远程 Agent 事件失败。"))
        return response.json()

    async def set_status(self, status: str) -> dict[str, Any]:
        return await self.send_event("status", status=status)

    async def download_memory(self, memory_key: str) -> bytes | None:
        """Download an optional opaque encrypted memory snapshot."""
        response = await self._agent_request(
            "GET",
            f"/v1/agents/{self.agent_id}/memory",
            params={"memory_key": memory_key},
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RemoteAgentError(_error(response, "下载云端 Agent 记忆失败。"))
        encoded = str(response.json().get("snapshot") or "")
        try:
            encrypted = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise RemoteAgentError("云端 Agent 记忆格式无效。") from exc
        checksum = str(response.json().get("checksum") or "")
        if hashlib.sha256(encrypted).hexdigest() != checksum:
            raise RemoteAgentError("云端 Agent 记忆校验失败。")
        return encrypted

    async def upload_memory(
        self, memory_key: str, encrypted_snapshot: bytes
    ) -> dict[str, Any]:
        """Upload an encrypted local snapshot; Relay never receives plaintext."""
        payload = {
            "snapshot": base64.b64encode(encrypted_snapshot).decode("ascii"),
            "checksum": hashlib.sha256(encrypted_snapshot).hexdigest(),
        }
        response = await self._agent_request(
            "PUT",
            f"/v1/agents/{self.agent_id}/memory",
            params={"memory_key": memory_key},
            json=payload,
        )
        if response.status_code >= 400:
            raise RemoteAgentError(_error(response, "上传云端 Agent 记忆失败。"))
        return response.json()

    async def send_progress(self, content: str, *, command_id: str) -> dict[str, Any]:
        return await self.send_event(
            "progress", content=content, in_reply_to=command_id
        )

    async def send_message(self, content: str, *, command_id: str) -> dict[str, Any]:
        content = content.strip()
        if not content:
            raise RemoteAgentError("远程 Agent 消息不能为空。")
        return await self.send_event("message", content=content, in_reply_to=command_id)

    async def send_error(self, content: str, *, command_id: str) -> dict[str, Any]:
        return await self.send_event("error", content=content, in_reply_to=command_id)

    async def serve(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        stop_event: asyncio.Event | None = None,
        wait_seconds: int = 30,
    ) -> None:
        """Wait for commands until stopped, retrying transient failures."""
        self._require_registration()
        stop = stop_event or asyncio.Event()
        backoff = 1.0
        try:
            await self.set_status("online")
            while not stop.is_set():
                try:
                    command = await self.wait_for_command(wait_seconds=wait_seconds)
                    backoff = 1.0
                    if command is not None:
                        await handler(command)
                except RemoteAgentError:
                    if stop.is_set():
                        break
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, 30.0)
        finally:
            try:
                await self.set_status("offline")
            except Exception:
                pass

    def _require_registration(self) -> None:
        if not self.agent_id or not self.agent_token:
            raise RemoteAgentError("请先运行 aero agent register 注册远程 Agent。")

    async def _agent_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self._require_registration()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self.agent_token}"
        try:
            return await self._agent_http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise RemoteAgentError("无法连接远程 Agent 服务，请检查网络。") from exc


async def renew_command_lease(
    client: RemoteAgentClient,
    command_id: str,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = 30.0,
) -> None:
    """Keep a running command lease alive until execution completes."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            await client.update_command_status(command_id, "running")


def _error(response: Any, fallback: str) -> str:
    try:
        body = response.json()
    except Exception:
        return fallback
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or fallback)
        return str(body.get("detail") or body.get("message") or fallback)
    return fallback
