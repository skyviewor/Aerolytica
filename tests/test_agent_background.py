import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from aero.cli import agent_background, agent_runner
from aero.core.remote_agent import RemoteAgentError


@pytest.mark.parametrize(
    "args",
    [
        ["example", "--background"],
        ["--background", "example"],
    ],
)
def test_background_flag_in_either_position(args):
    assert agent_runner._background_args(args) == ("example", True)


def test_invalid_background_options():
    with pytest.raises(ValueError):
        agent_runner._background_args(["example", "--background", "--background"])
    with pytest.raises(ValueError):
        agent_runner._background_args(["example", "--bad"])


async def test_one_step_background_registers_before_launch(monkeypatch):
    connection = Mock()
    connection.registry.list.return_value = []
    connection.close = AsyncMock()
    monkeypatch.setattr(agent_runner, "RemoteAgentClient", Mock(return_value=connection))
    register = AsyncMock(return_value=0)
    launch = Mock(return_value=0)
    foreground = AsyncMock()
    monkeypatch.setattr(agent_runner, "_register", register)
    monkeypatch.setattr(agent_runner, "_start_background_service", launch)
    monkeypatch.setattr(agent_runner, "_run_service", foreground)
    assert await agent_runner._start_service("example", background=True) == 0
    register.assert_awaited_once_with("example", "")
    launch.assert_called_once_with("example")
    foreground.assert_not_awaited()


def test_launcher_detaches_real_test_process_and_keeps_private_log(tmp_path, monkeypatch):
    # Exercise real OS detachment/IPC using a harmless worker, never a cloud Agent.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original_popen = subprocess.Popen
    children = []

    def popen(command, **kwargs):
        assert command[:4] == [sys.executable, "-u", "-m", "aero.cli.agent_background"]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["start_new_session"] is True
        fd = command[-1]
        child = original_popen(
            [
                sys.executable,
                "-c",
                f"import os,time; os.write({fd},b'1'); os.close({fd}); time.sleep(30)",
            ],
            **kwargs,
        )
        children.append(child)
        return child

    monkeypatch.setattr(agent_background.subprocess, "Popen", popen)
    try:
        pid, log = agent_background.launch_background_agent("test-agent", tmp_path)
        assert pid == children[0].pid
        assert os.getsid(pid) == pid
        assert log.stat().st_mode & 0o777 == 0o600
        assert log.parent.stat().st_mode & 0o777 == 0o700
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=3)


def test_failed_child_is_not_reported_as_started(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    process = Mock()
    process.poll.return_value = 1
    monkeypatch.setattr(agent_background.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(agent_background.select, "select", lambda *_: ([], [], []))
    with pytest.raises(RemoteAgentError, match="未能启动"):
        agent_background.launch_background_agent("test-agent", tmp_path)


def test_worker_does_not_acknowledge_or_run_if_agent_lock_is_held(monkeypatch):
    lock = Mock()
    lock.acquire.side_effect = RemoteAgentError("已在本机运行")
    monkeypatch.setattr(agent_background, "AgentProcessLock", Mock(return_value=lock))
    run = AsyncMock()
    monkeypatch.setattr(agent_runner, "_run_service", run)
    monkeypatch.setattr(sys, "argv", ["worker", "test-agent", "--ready-fd", "99"])
    write = Mock()
    monkeypatch.setattr(agent_background.os, "write", write)
    assert agent_background.main() == 1
    write.assert_not_called()
    run.assert_not_awaited()
    lock.release.assert_called_once()


def test_worker_confirms_process_lock_and_reuses_it(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    lock = Mock()
    monkeypatch.setattr(agent_background, "AgentProcessLock", Mock(return_value=lock))

    async def started(*args, **kwargs):
        kwargs["on_ready"]()
        return 0

    run = AsyncMock(side_effect=started)
    monkeypatch.setattr(agent_runner, "_run_service", run)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(sys, "argv", ["worker", "test-agent", "--ready-fd", str(write_fd)])
    try:
        assert agent_background.main() == 0
        assert os.read(read_fd, 1) == b"1"
    finally:
        os.close(read_fd)
    lock.acquire.assert_called_once()
    assert run.await_args.args == ("test-agent",)
    assert run.await_args.kwargs["process_lock"] is lock
    lock.release.assert_called_once()


def test_stop_real_detached_test_worker_and_repeat_safely(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original_popen = subprocess.Popen
    children = []
    code = f"""
import asyncio, signal
from pathlib import Path
Path.home = lambda: Path({str(tmp_path)!r})
from aero.cli import agent_background, agent_runner
async def fake_run(selector, *, process_lock, on_ready, on_runtime=None):
    stopped = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopped.set)
    on_ready()
    await stopped.wait()
    return 0
agent_runner._run_service = fake_run
raise SystemExit(agent_background.main())
"""

    def popen(command, **kwargs):
        child = original_popen([sys.executable, "-c", code, *command[4:]], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(agent_background.subprocess, "Popen", popen)
    agent_id = "isolated-stop-" + tmp_path.name
    try:
        agent_background.launch_background_agent(agent_id, tmp_path)
        assert agent_background.stop_background_agent(agent_id)
        assert children[0].wait(timeout=3) == 0
        assert not agent_background.stop_background_agent(agent_id)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)
