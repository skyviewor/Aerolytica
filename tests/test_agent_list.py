from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from aero.cli import agent_runner
from aero.core.remote_agent import RemoteAgentClient


def client(records, response=None, error=None):
    value = RemoteAgentClient.__new__(RemoteAgentClient)
    value.registry = SimpleNamespace(list=Mock(return_value=records))
    value.session = SimpleNamespace(request=AsyncMock(return_value=response, side_effect=error))
    return value


def records():
    return [
        SimpleNamespace(agent_id=f"a{i}", name=f"local_{i}", description="", token="secret")
        for i in range(4)
    ]


async def test_list_includes_every_local_agent_but_not_cloud_only_agents():
    value = client(
        records(),
        httpx.Response(
            200,
            json={
                "agents": [
                    {"agent_id": "a0", "name": "remote_display", "status": "online"},
                    {"agent_id": "a1", "status": "busy"},
                    {"agent_id": "a2", "status": "offline"},
                    {"agent_id": "cloud-only", "status": "online"},
                ]
            },
        ),
    )
    result = await value.list_registered_remote_agents()
    assert [item["name"] for item in result] == [f"local_{i}" for i in range(4)]
    assert [item["status"] for item in result] == ["online", "busy", "offline", "unavailable"]
    assert "secret" not in str(result)


@pytest.mark.parametrize("status", [401, 500])
async def test_server_errors_keep_local_list_with_unknown_status(status):
    value = client(records(), httpx.Response(status))
    result = await value.list_registered_remote_agents()
    assert len(result) == 4
    assert all(item["status"] == "unknown" and item["status_error"] for item in result)


async def test_offline_does_not_report_agents_as_offline():
    value = client(records(), error=httpx.ConnectError("offline"))
    result = await value.list_registered_remote_agents()
    assert all(item["status"] == "unknown" for item in result)


async def test_empty_local_registry_does_not_require_network():
    value = client([])
    assert await value.list_registered_remote_agents() == []
    value.session.request.assert_not_awaited()


async def test_cli_displays_chinese_status_and_closes_connection(monkeypatch, capsys):
    value = SimpleNamespace(
        list_registered_remote_agents=AsyncMock(
            return_value=[
                {"name": "test_agent", "agent_id": "a", "status": "busy"},
                {"name": "offline_agent", "agent_id": "b", "status": "offline"},
            ]
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(agent_runner, "RemoteAgentClient", Mock(return_value=value))
    assert await agent_runner._list_agents() == 0
    output = capsys.readouterr().out
    assert "在线（执行中）" in output and "离线" in output and "智能体 ID" in output
    value.close.assert_awaited_once()


async def test_cli_query_failure_prints_records_and_returns_nonzero(monkeypatch, capsys):
    value = SimpleNamespace(
        list_registered_remote_agents=AsyncMock(
            return_value=[
                {
                    "name": "saved_agent",
                    "agent_id": "a",
                    "status": "unknown",
                    "status_error": "网络失败。",
                },
            ]
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(agent_runner, "RemoteAgentClient", Mock(return_value=value))
    assert await agent_runner._list_agents() == 1
    output = capsys.readouterr().out
    assert "saved_agent" in output and "状态未知" in output and "网络失败" in output
