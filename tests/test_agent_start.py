from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aero.cli import agent_runner
from aero.core.remote_agent import RemoteAgentError


def client(monkeypatch, records):
    value = SimpleNamespace(
        registry=SimpleNamespace(list=Mock(return_value=records)),
        close=AsyncMock(),
    )
    monkeypatch.setattr(agent_runner, "RemoteAgentClient", Mock(return_value=value))
    return value


async def test_start_registers_missing_name_then_runs(monkeypatch):
    connection = client(monkeypatch, [])
    register, run = AsyncMock(return_value=0), AsyncMock(return_value=0)
    monkeypatch.setattr(agent_runner, "_register", register)
    monkeypatch.setattr(agent_runner, "_run_service", run)
    assert await agent_runner._start_service("new_agent") == 0
    register.assert_awaited_once_with("new_agent", "")
    run.assert_awaited_once_with("new_agent")
    connection.close.assert_awaited_once()


@pytest.mark.parametrize("selector", ["existing_agent", "EXISTING_AGENT", "agent_existing"])
async def test_start_reuses_existing_identity(monkeypatch, selector):
    client(monkeypatch, [SimpleNamespace(agent_id="agent_existing", name="existing_agent")])
    register, run = AsyncMock(), AsyncMock(return_value=0)
    monkeypatch.setattr(agent_runner, "_register", register)
    monkeypatch.setattr(agent_runner, "_run_service", run)
    assert await agent_runner._start_service(selector) == 0
    register.assert_not_awaited()
    run.assert_awaited_once_with("agent_existing")


async def test_registration_error_does_not_start(monkeypatch):
    client(monkeypatch, [])
    monkeypatch.setattr(
        agent_runner,
        "_register",
        AsyncMock(side_effect=RemoteAgentError("网络失败")),
    )
    run = AsyncMock()
    monkeypatch.setattr(agent_runner, "_run_service", run)
    with pytest.raises(RemoteAgentError):
        await agent_runner._start_service("new_agent")
    run.assert_not_awaited()


def test_start_requires_explicit_name(capsys):
    assert agent_runner.run_agent_cli(["start"]) == 1
    assert "aero agent start 智能体名称" in capsys.readouterr().out
