from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from aero.cli import agent_runner
from aero.core.remote_agent import RemoteAgentError, RemoteAgentRegistry


def setup_client(monkeypatch, status=200):
    value = SimpleNamespace(
        agent_name="selected",
        agent_id="agent_selected",
        registry=SimpleNamespace(remove=Mock()),
        session=SimpleNamespace(request=AsyncMock(return_value=httpx.Response(status))),
        close=AsyncMock(),
    )
    monkeypatch.setattr(agent_runner, "RemoteAgentClient", Mock(return_value=value))
    return value


async def test_delete_cancel_does_not_stop_request_or_remove(monkeypatch):
    value = setup_client(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    stop = Mock()
    monkeypatch.setattr(agent_runner, "_stop_service", stop)
    assert await agent_runner._delete_agent("selected") == 0
    stop.assert_not_called()
    value.session.request.assert_not_awaited()
    value.registry.remove.assert_not_called()
    value.close.assert_awaited_once()


async def test_delete_stops_then_deletes_only_selected_identity(monkeypatch):
    value = setup_client(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "selected")
    stop = Mock(return_value=0)
    monkeypatch.setattr(agent_runner, "_stop_service", stop)
    assert await agent_runner._delete_agent("selected") == 0
    stop.assert_called_once_with("agent_selected")
    value.session.request.assert_awaited_once_with("DELETE", "/v1/agents/agent_selected")
    value.registry.remove.assert_called_once_with("agent_selected")


@pytest.mark.parametrize("status", [401, 404, 503])
async def test_delete_failure_preserves_registration(monkeypatch, status):
    value = setup_client(monkeypatch, status)
    monkeypatch.setattr("builtins.input", lambda _: "selected")
    monkeypatch.setattr(agent_runner, "_stop_service", Mock(return_value=0))
    with pytest.raises(RemoteAgentError, match="凭据已保留"):
        await agent_runner._delete_agent("selected")
    value.registry.remove.assert_not_called()


async def test_delete_does_not_proceed_if_worker_cannot_stop(monkeypatch):
    value = setup_client(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "selected")
    monkeypatch.setattr(agent_runner, "_stop_service", Mock(side_effect=RemoteAgentError("仍运行")))
    with pytest.raises(RemoteAgentError):
        await agent_runner._delete_agent("selected")
    value.session.request.assert_not_awaited()
    value.registry.remove.assert_not_called()


def test_registry_remove_preserves_other_registration(tmp_path, monkeypatch):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    registry = RemoteAgentRegistry()
    registry.add(agent_id="agent_one", name="one", description="", token="test-one")
    registry.add(agent_id="agent_two", name="two", description="", token="test-two")
    registry.remove("agent_one")
    assert [item.agent_id for item in registry.list()] == ["agent_two"]
    assert registry.resolve("two").token == "test-two"


@pytest.mark.parametrize("command", ["stop", "delete"])
def test_destructive_targets_must_be_explicit(command, capsys):
    assert agent_runner.run_agent_cli([command]) == 1
    assert f"aero agent {command}" in capsys.readouterr().out
