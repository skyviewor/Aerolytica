"""Tests for the Relay remote-Agent HTTP boundary."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from aero.agent.session import SessionManager
from aero.cli.agent_runner import (
    _memory_session_id,
    _restore_agent_memory,
    _run_args,
    _save_agent_memory,
    _selector_arg,
)
from aero.core.remote_agent import (
    AgentProcessLock,
    RemoteAgentClient,
    RemoteAgentError,
    RemoteAgentRegistry,
    renew_command_lease,
)
from aero.core.types import Message


class Session:
    base_url = "https://relay.test"

    def __init__(self, transport: httpx.MockTransport):
        self.transport = transport

    async def request(self, method, path, **kwargs):
        async with httpx.AsyncClient(
            transport=self.transport, base_url=self.base_url
        ) as client:
            return await client.request(method, path, **kwargs)

    async def close(self):
        return None


def test_remote_agent_cli_uses_positional_agent_selector():
    assert _run_args(["ocean"]) == "ocean"
    assert _selector_arg(["ocean"], "status") == "ocean"
    with pytest.raises(ValueError, match="用法"):
        _selector_arg(["--agent", "ocean"], "status")


@pytest.mark.asyncio
async def test_remote_agent_register_wait_and_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/v1/agents/register":
            return httpx.Response(
                200,
                json={
                    "agent_id": "agent_1",
                    "name": "ocean",
                    "description": "测试 Agent",
                    "token": "agt_token",
                    "status": "offline",
                },
                request=request,
            )
        if request.url.path.endswith("/commands/next"):
            return httpx.Response(
                200,
                json={
                    "command": {
                        "command_id": "cmd_1",
                        "message_id": "msg_1",
                        "content": "列出文件",
                    }
                },
                request=request,
            )
        if request.url.path.endswith("/memory") and request.method == "GET":
            encrypted = b"encrypted-memory"
            return httpx.Response(
                200,
                json={
                    "snapshot": base64.b64encode(encrypted).decode(),
                    "checksum": hashlib.sha256(encrypted).hexdigest(),
                },
                request=request,
            )
        return httpx.Response(200, json={"status": "ok"}, request=request)

    transport = httpx.MockTransport(handler)
    agent_http = httpx.AsyncClient(transport=transport, base_url="https://relay.test")
    client = RemoteAgentClient(
        Session(transport), agent_http=agent_http, select_agent=False
    )
    registration = await client.register("ocean", description="测试 Agent")
    assert registration["status"] == "offline"
    command = await client.wait_for_command()
    assert command["content"] == "列出文件"
    await client.send_progress("处理中", command_id="cmd_1")
    await client.send_message("完成", command_id="cmd_1")
    await client.upload_memory("remote-test", b"encrypted-memory")
    assert await client.download_memory("remote-test") == b"encrypted-memory"
    memory_upload = next(
        call for call in calls if call[0] == "PUT" and call[1].endswith("/memory")
    )
    assert memory_upload[2] == "Bearer agt_token"
    assert all(
        auth == "Bearer agt_token"
        for method, path, auth in calls
        if path != "/v1/agents/register"
    )
    await client.close()


def test_remote_agent_registry_supports_multiple_agents_and_name_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    registry = RemoteAgentRegistry()
    first = registry.add(
        agent_id="agent_1", name="ocean", description="海洋 Agent", token="agt_1"
    )
    second = registry.add(
        agent_id="agent_2", name="paper", description="论文 Agent", token="agt_2"
    )
    assert [agent.name for agent in registry.list()] == ["ocean", "paper"]
    assert registry.resolve("OCEAN") == first
    assert registry.resolve("agent_2") == second
    with pytest.raises(RemoteAgentError, match="多个 Agent"):
        registry.resolve()
    with pytest.raises(RemoteAgentError, match="已存在"):
        registry.add(
            agent_id="agent_3", name="ocean", description="重复", token="agt_3"
        )
    for invalid_name in ("Ocean", "ocean-data", "ocean data"):
        with pytest.raises(RemoteAgentError, match="小写英文"):
            registry.ensure_name_available(invalid_name)


def test_remote_agent_process_lock_is_per_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    first = AgentProcessLock("agent_1")
    second = AgentProcessLock("agent_1")
    other = AgentProcessLock("agent_2")
    first.acquire()
    try:
        with pytest.raises(RemoteAgentError, match="本机运行"):
            second.acquire()
        other.acquire()
        other.release()
    finally:
        first.release()


@pytest.mark.asyncio
async def test_local_agent_memory_is_encrypted_and_restored(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = SessionManager(tmp_path / "agent-memory")
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    class Client:
        agent_id = "agent_1"
        agent_name = "测试 Agent"

    class Agent:
        def __init__(self, messages):
            self.messages = messages
            self.config = SimpleNamespace(
                language="zh",
                llm=SimpleNamespace(model="test-model", provider="test-provider"),
                mode="execute",
            )

        def reset_system_prompt(self, language):
            self.messages[0] = Message(role="system", content=f"system-{language}")

    client = Client()
    session_id = _memory_session_id(client.agent_id, project_dir)
    original = Agent(
        [
            Message(role="system", content="system"),
            Message(role="user", content="需要持久化的上下文"),
            Message(role="assistant", content="已经记住"),
        ]
    )
    await _save_agent_memory(
        client,
        original,
        manager,
        session_id,
        project_dir,
    )
    encrypted = manager.snapshot(session_id)
    assert encrypted is not None
    assert "需要持久化的上下文".encode() not in encrypted

    restored = Agent([Message(role="system", content="new")])
    source = await _restore_agent_memory(
        client,
        restored,
        manager,
        session_id,
    )
    assert source == "本地记忆"
    assert [message.content for message in restored.messages[1:]] == [
        "需要持久化的上下文",
        "已经记住",
    ]


@pytest.mark.asyncio
async def test_remote_agent_serve_uses_long_poll(tmp_path, monkeypatch):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    (tmp_path / "secrets.yaml").write_text(
        "remote_agents:\n"
        "  agents:\n"
        "    agent_1:\n"
        "      name: ocean\n"
        "      description: 测试 Agent\n"
        "      token: agt_token\n",
        encoding="utf-8",
    )
    commands = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal commands
        if request.url.path.endswith("/commands/next"):
            commands += 1
            return httpx.Response(
                200,
                json={"command": {"command_id": "cmd_1", "content": "执行任务"}},
                request=request,
            )
        return httpx.Response(200, json={"status": "ok"}, request=request)

    transport = httpx.MockTransport(handler)
    client = RemoteAgentClient(
        Session(transport),
        agent_http=httpx.AsyncClient(transport=transport, base_url="https://relay.test"),
    )
    handled = []
    stop = asyncio.Event()

    async def handle(command):
        handled.append(command["content"])
        stop.set()

    await client.serve(handle, stop_event=stop, wait_seconds=0)
    assert commands == 1
    assert handled == ["执行任务"]
    await client.close()


@pytest.mark.asyncio
async def test_lease_renewal_posts_running_status(tmp_path, monkeypatch):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    (tmp_path / "secrets.yaml").write_text(
        "remote_agents:\n  agents:\n    agent_1:\n      name: ocean\n      token: agt_token\n",
        encoding="utf-8",
    )
    renewals = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal renewals
        if request.url.path.endswith("/status"):
            renewals += 1
        return httpx.Response(200, json={"status": "running"}, request=request)

    transport = httpx.MockTransport(handler)
    client = RemoteAgentClient(
        Session(transport),
        agent_http=httpx.AsyncClient(transport=transport, base_url="https://relay.test"),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(
        renew_command_lease(client, "cmd_1", stop, interval_seconds=0.01)
    )
    await asyncio.sleep(0.025)
    stop.set()
    await task
    assert renewals >= 1
    await client.close()
