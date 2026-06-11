"""Tests for the public managed runtime interface."""

import subprocess

from meteora.toolbox.runtime_manager import RuntimeToolManager


def test_runtime_manager_detects_managed_commands():
    manager = RuntimeToolManager()

    assert manager.managed_tools_in_command("cdo mergetime a.nc b.nc") == ["cdo"]
    assert manager.managed_tools_in_command("echo cdo.txt") == []


def test_runtime_manager_uses_injected_command_runner(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="meteora-agent ready\n", stderr="")

    manager = RuntimeToolManager(command_runner=fake_run)
    conda = tmp_path / "bin" / "conda"
    conda.parent.mkdir()
    conda.write_text("#!/bin/sh\n")

    assert manager.conda_env_exists(str(conda), {"PATH": str(conda.parent)}) is True
    assert calls[0][0] == [str(conda), "env", "list"]
