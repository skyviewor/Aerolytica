from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aero.cli import agent_background, chat_host
from aero.core.remote_agent import RemoteAgentError


def setup_host(monkeypatch, *, empty=False):
    registry = Mock()
    registry.ensure_name_available.side_effect = lambda name: name
    monkeypatch.setattr(chat_host, "RemoteAgentRegistry", lambda: registry)
    result = {
        "agent_id": "original_agent",
        "name": "my_agent",
        "token": "rotated",
        "session_id": "original_session",
        "project_id": None,
    }
    response = SimpleNamespace(status_code=200, json=lambda: result)
    presence = SimpleNamespace(
        agent_id="" if empty else "original_agent",
        token="original",
        session=SimpleNamespace(
            request=AsyncMock(return_value=response), data=SimpleNamespace(is_logged_in=True)
        ),
        _active_session_id="original_session",
        _active_command_id=None,
        is_idle=lambda: True,
        close=AsyncMock(),
        title=lambda: "原对话标题",
    )
    launch = Mock(return_value=(123, Path("/tmp/isolated.log")))
    monkeypatch.setattr(agent_background, "launch_background_agent", launch)
    return registry, presence, launch


async def test_host_keeps_same_ids_and_saves_credentials_before_launch(monkeypatch, tmp_path):
    registry, presence, launch = setup_host(monkeypatch)

    def launched(*args):
        registry.add.assert_called_once()
        presence.close.assert_awaited_once()
        return 123, tmp_path / "log"

    launch.side_effect = launched
    result = await chat_host.host_chat(presence, "my_agent", tmp_path, "original_session")
    assert result["agent_id"] == "original_agent"
    assert result["session_id"] == "original_session"
    launch.assert_called_once_with("original_agent", tmp_path)
    assert presence.session.request.await_args.args[1].endswith("/chat-host")


async def test_empty_chat_explicitly_registers_original_session(monkeypatch, tmp_path):
    _, presence, _ = setup_host(monkeypatch, empty=True)
    await chat_host.host_chat(presence, "my_agent", tmp_path, "original_session")
    first = presence.session.request.await_args_list[0]
    assert first.args[1] == "/v1/agents/chats"
    assert first.kwargs["json"]["session_id"] == "original_session"


async def test_busy_chat_is_not_transferred(monkeypatch, tmp_path):
    registry, presence, launch = setup_host(monkeypatch)
    presence._active_command_id = "cmd"
    with pytest.raises(RemoteAgentError, match="任务"):
        await chat_host.host_chat(presence, "my_agent", tmp_path, "original_session")
    launch.assert_not_called()
    registry.add.assert_not_called()
    presence.close.assert_not_awaited()


async def test_failed_launch_keeps_registration_and_gives_resume_command(monkeypatch, tmp_path):
    registry, presence, launch = setup_host(monkeypatch)
    launch.side_effect = RemoteAgentError("isolated failure")
    with pytest.raises(RemoteAgentError, match="aero agent run my_agent --background"):
        await chat_host.host_chat(presence, "my_agent", tmp_path, "original_session")
    registry.add.assert_called_once()


async def test_server_reject_preserves_foreground_and_never_launches(monkeypatch, tmp_path):
    registry, presence, launch = setup_host(monkeypatch)
    presence.session.request.return_value = SimpleNamespace(
        status_code=409, json=lambda: {"detail": "名额不足"}
    )
    with pytest.raises(RemoteAgentError, match="名额不足"):
        await chat_host.host_chat(presence, "my_agent", tmp_path, "original_session")
    registry.add.assert_not_called()
    presence.close.assert_not_awaited()
    launch.assert_not_called()


@pytest.mark.parametrize("command", ["/host my_agent", "/agent background my_agent"])
async def test_tui_persists_empty_session_before_handoff_and_exits_only_after_success(
    monkeypatch,
    tmp_path,
    command,
):
    from aero.cli.main import AeroApp
    from aero.core.config import AeroConfig

    manager = Mock()
    manager.load.return_value = None
    app = SimpleNamespace(
        agent=SimpleNamespace(messages=[]),
        _agent_worker=None,
        _subagent_workers={},
        _get_experiment_mgr=lambda: SimpleNamespace(active=lambda: None),
        _cloud_chat_session_id=lambda: "empty_session",
        _auto_save_session=Mock(),
        _get_session_mgr=lambda: manager,
        _cloud_chat_title=lambda: "空对话",
        _project_dir=tmp_path,
        config=AeroConfig.create_default(),
        _chat_presence=Mock(),
        notify=Mock(),
        exit=Mock(),
        _session_saved_on_exit=False,
    )

    async def handoff(*args):
        manager.save.assert_called_once()
        assert manager.save.call_args.args[0] == "empty_session"
        return {"pid": 42, "log": "isolated.log"}

    monkeypatch.setattr(chat_host, "host_chat", handoff)
    await AeroApp._host_current_chat(app, command)
    assert app._chat_presence is None
    assert app._session_saved_on_exit
    app.exit.assert_called_once()


@pytest.mark.parametrize(
    "command",
    [
        "/agent",
        "/agent background",
        "/agent stop my_agent",
        "/agent background my_agent extra",
        "/host",
        "/host my_agent extra",
    ],
)
def test_invalid_host_commands_show_usage(command):
    with pytest.raises(ValueError, match="/agent background my_agent"):
        chat_host.parse_host_command(command)


async def test_tui_failure_after_conversion_exits_instead_of_resuming_foreground(
    monkeypatch,
    tmp_path,
):
    from aero.cli.main import AeroApp

    app = SimpleNamespace(
        agent=SimpleNamespace(messages=[]),
        _agent_worker=None,
        _subagent_workers={},
        _get_experiment_mgr=lambda: SimpleNamespace(active=lambda: None),
        _cloud_chat_session_id=lambda: "saved",
        _auto_save_session=Mock(),
        _get_session_mgr=lambda: SimpleNamespace(load=lambda sid: object()),
        _project_dir=tmp_path,
        _chat_presence=Mock(),
        notify=Mock(),
        exit=Mock(),
    )
    monkeypatch.setattr(
        chat_host, "host_chat", AsyncMock(side_effect=chat_host.HostLaunchError("恢复命令"))
    )
    await AeroApp._host_current_chat(app, "/host my_agent")
    app.exit.assert_called_once_with(message="恢复命令")
    assert app._chat_presence is None
