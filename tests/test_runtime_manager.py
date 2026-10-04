"""Tests for the public managed runtime interface."""

import asyncio
import os

import pytest

from aero.toolbox.runtime_manager import RuntimeToolManager


def test_runtime_manager_detects_managed_commands():
    manager = RuntimeToolManager()

    assert manager.managed_tools_in_command("cdo mergetime a.nc b.nc") == ["cdo"]
    assert manager.managed_tools_in_command("echo cdo.txt") == []


def test_runtime_manager_checks_private_environment_prefix(monkeypatch, tmp_path):
    root = tmp_path / "runtime"
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))
    manager = RuntimeToolManager()
    micromamba = root / "bin" / "micromamba"

    assert manager.conda_env_exists(str(micromamba), {}) is False

    (root / "envs" / "aero-agent" / "conda-meta").mkdir(parents=True)
    assert manager.conda_env_exists(str(micromamba), {}) is True


@pytest.mark.asyncio
async def test_runtime_manager_cancellation_terminates_process_group(tmp_path):
    parent_pid_file = tmp_path / "parent.pid"
    child_pid_file = tmp_path / "child.pid"
    command = [
        "/bin/sh",
        "-c",
        f"trap '' TERM; echo $$ > {parent_pid_file}; sleep 60 & echo $! > {child_pid_file}; wait",
    ]
    manager = RuntimeToolManager()
    task = asyncio.create_task(manager.run_command_async(command, env=dict(os.environ), timeout=60))

    for _ in range(100):
        if parent_pid_file.exists() and child_pid_file.exists():
            break
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for pid_file in (parent_pid_file, child_pid_file):
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
