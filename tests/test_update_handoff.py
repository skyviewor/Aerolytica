from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from aero.cli import agent_background, update
from aero.cli.main import AeroApp
from aero.cli.update_ui import idle
from aero.core.config import AeroConfig
from aero.server.app import create_app


def test_update_prompt_waits_for_empty_composer_and_idle_resident():
    editor = SimpleNamespace(text="正在输入")
    app = SimpleNamespace(query_one=lambda _: editor, screen_stack=[object()])
    assert not idle(app)
    editor.text = ""
    assert idle(app)
    app.conversation = SimpleNamespace(agent={"status": "online"})
    app._approvals = {}
    app._booted = False
    assert not idle(app)
    app._booted = True
    assert idle(app)
    app.conversation.agent["status"] = "busy"
    assert not idle(app)
    app.conversation.agent["status"] = "online"
    app._approvals["test"] = {"status": "pending"}
    assert not idle(app)


def test_rollback_rejects_external_environment_before_smoke(tmp_path, monkeypatch):
    check = Mock()
    monkeypatch.setattr(update, "install_root", lambda: tmp_path / "install")
    monkeypatch.setattr(update, "smoke", check)
    with pytest.raises(update.DistributionError, match="不属于本安装"):
        update._smoke_owned_environment(tmp_path / "untrusted")
    check.assert_not_called()


@pytest.mark.parametrize("busy", [True, False])
def test_private_agent_socket_refuses_busy_stop_and_fences_new_work(tmp_path, monkeypatch, busy):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original = subprocess.Popen
    children = []
    code = f"""
import asyncio, signal
from pathlib import Path
from types import SimpleNamespace
Path.home = lambda: Path({str(tmp_path)!r})
from aero.cli import agent_background, agent_runner
class Runtime:
    update_paused = False
    session = SimpleNamespace(update_paused=False)
    def status(self):
        return {{"update_busy": {busy!r}, "update_paused": self.update_paused}}
async def run(selector, *, process_lock, on_ready, on_runtime):
    stopped = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopped.set)
    on_runtime(Runtime())
    on_ready()
    await stopped.wait()
    return 0
agent_runner._run_service = run
raise SystemExit(agent_background.main())
"""

    def launch(command, **kwargs):
        child = original([sys.executable, "-c", code, *command[4:]], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(agent_background.subprocess, "Popen", launch)
    identifier = "isolated-upgrade-" + tmp_path.name
    record = {"kind": "agent", "control": {"agent_id": identifier}}
    try:
        agent_background.launch_background_agent(identifier, tmp_path)
        state = update.control(record, "drain")
        assert state == {"protocol": 1, "update_busy": busy, "update_paused": True}
        stopping = update.control(record, "update-stop")
        assert stopping["stopping"] is not busy
        if busy:
            assert children[0].poll() is None
            assert not update.control(record, "resume")["update_paused"]
            agent_background.stop_background_agent(identifier)
        assert children[0].wait(timeout=3) == 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()  # Only this test's own harmless child.
            child.wait(timeout=3)


def test_daemon_restart_preserves_agents_sessions_and_port(tmp_path, monkeypatch):
    manager = Mock()
    manager.connection_info.return_value = {
        "base_url": "http://127.0.0.1:43210",
        "token": "test-private-capability",
    }
    monkeypatch.setattr("aero.core.daemon.DaemonManager", Mock(return_value=manager))
    response = Mock()
    response.json.return_value = {"update_paused": True}
    post = Mock(return_value=response)
    monkeypatch.setattr("httpx.post", post)
    record = {
        "kind": "daemon",
        "control": {
            "project_dir": str(tmp_path),
            "state_dir": str(tmp_path / "state"),
            "port": 43210,
        },
        "hosted_agents": [
            {
                "agent_id": "agent_test",
                "session_id": "original_session",
                "project_id": "project_test",
            }
        ],
    }
    update._restart(record, tmp_path / "new-version")
    manager.start.assert_called_once_with(
        port=43210, interpreter=str(tmp_path / "new-version/bin/python")
    )
    assert post.call_args.kwargs["json"] == {
        "project_id": "project_test",
        "session_id": "original_session",
        "update_restore": True,
    }
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer test-private-capability"}
    assert post.call_args.kwargs["trust_env"] is False


def test_daemon_restore_only_accepts_paused_native_controller(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    app, runtime = create_app(tmp_path, launch_token="test-capability")
    client = TestClient(app)
    client.cookies.set("aero_access", "test-capability")
    runtime.update_paused = True
    body = {"project_id": "project", "session_id": "session"}
    url = "/api/v1/cloud/agents/agent_test/start"
    assert client.post(url, json=body).status_code == 503
    assert client.post(url, json={**body, "update_restore": True}).status_code == 403
    assert (
        client.post(
            url,
            json={**body, "update_restore": True},
            headers={"Authorization": "Bearer test-capability", "Origin": "http://localhost"},
        ).status_code
        == 403
    )
    # Native authorization passes the upgrade fence, but cannot invent an Agent.
    assert (
        client.post(
            url,
            json={**body, "update_restore": True},
            headers={"Authorization": "Bearer test-capability"},
        ).status_code
        == 404
    )


async def test_update_restores_exact_session_not_latest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    monkeypatch.setattr("aero.application.chat_presence.ChatPresence.run", AsyncMock())
    monkeypatch.setattr(AeroApp, "_refresh_official_credits", AsyncMock())
    sessions = Mock()
    sessions.load.return_value = ([], SimpleNamespace(project_dir=str(tmp_path)))
    monkeypatch.setattr(AeroApp, "_get_session_mgr", lambda _: sessions)
    load = Mock()
    latest = Mock()
    monkeypatch.setattr(AeroApp, "_do_load_session", load)
    monkeypatch.setattr(AeroApp, "_resume_latest_project_session", latest)
    app = AeroApp(
        AeroConfig.create_default(),
        persist_config=False,
        resume_last_session=True,
        resume_session_id="the-current-session",
    )
    async with app.run_test():
        load.assert_called_once_with("the-current-session")
        latest.assert_not_called()
