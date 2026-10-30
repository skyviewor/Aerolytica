"""Installer diagnostics and failover use simulated registries only."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from aero import distribution as dist


@pytest.fixture(autouse=True)
def isolated_indexes(monkeypatch):
    for name in (
        "UV_DEFAULT_INDEX",
        "UV_INDEX_URL",
        "UV_INDEX",
        "UV_EXTRA_INDEX_URL",
        "UV_FIND_LINKS",
        "UV_NO_INDEX",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AERO_NETWORK_REGION", "mainland_china")


def test_forbidden_mirror_fails_over_without_weakening_integrity(monkeypatch, capsys):
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 1, "", "403 Forbidden https://private:secret@host/"),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(dist.subprocess, "run", run)
    dist._sync_dependencies(Path("/uv"), Path("/python"), Path("/locked.txt"))
    assert run.call_count == 2
    first, second = run.call_args_list
    assert first.kwargs["env"]["UV_DEFAULT_INDEX"] == "https://pypi.tuna.tsinghua.edu.cn/simple"
    assert second.kwargs["env"]["UV_DEFAULT_INDEX"] == "https://mirrors.aliyun.com/pypi/simple"
    assert first.args == second.args
    command = second.args[0]
    assert "--require-hashes" in command
    assert "--only-binary" in command
    assert "--no-config" in command
    assert "--allow-insecure-host" not in command
    assert second.kwargs["timeout"] == 180
    output = capsys.readouterr().err
    assert "HTTP 403" in output
    assert "切换备用" in output
    assert "secret" not in output and "private" not in output


@pytest.mark.parametrize("failure", ["Hash mismatch for package", "No space left on device"])
def test_integrity_and_disk_errors_do_not_switch_sources(monkeypatch, failure):
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", failure))
    monkeypatch.setattr(dist.subprocess, "run", run)
    with pytest.raises(dist.InstallCommandError):
        dist._sync_dependencies(Path("/uv"), Path("/python"), Path("/locked.txt"))
    assert run.call_count == 1


@pytest.mark.parametrize("name", ["UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL"])
def test_explicit_registry_is_respected_without_silent_fallback(monkeypatch, name):
    monkeypatch.setenv(name, "https://user:secret@private.example/simple")
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", "403 Forbidden"))
    monkeypatch.setattr(dist.subprocess, "run", run)
    with pytest.raises(dist.InstallCommandError, match="HTTP 403") as error:
        dist._sync_dependencies(Path("/uv"), Path("/python"), Path("/locked.txt"))
    assert run.call_count == 1
    assert run.call_args.kwargs["env"][name] == "https://user:secret@private.example/simple"
    assert "secret" not in str(error.value)


def test_all_sources_fail_with_bounded_retries_and_safe_diagnostic(monkeypatch):
    run = Mock(side_effect=subprocess.TimeoutExpired(["uv", "private-token"], 180))
    monkeypatch.setattr(dist.subprocess, "run", run)
    with pytest.raises(dist.InstallCommandError, match="安装锁定依赖超时") as error:
        dist._sync_dependencies(Path("/uv"), Path("/python"), Path("/locked.txt"))
    assert run.call_count == 3
    assert "private-token" not in str(error.value)


def test_non_mainland_prefers_official_registry(monkeypatch):
    monkeypatch.setenv("AERO_NETWORK_REGION", "global")
    assert dist._dependency_indexes() == [
        "https://pypi.org/simple",
        "https://mirrors.aliyun.com/pypi/simple",
    ]


def test_creation_failure_identifies_step_without_dumping_output(monkeypatch):
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [],
            1,
            "token=secret /Users/private/path",
            "Permission denied",
        )
    )
    monkeypatch.setattr(dist.subprocess, "run", run)
    monkeypatch.setenv("CONDA_PREFIX", "/existing/conda")
    with pytest.raises(dist.InstallCommandError, match="创建私有虚拟环境失败") as error:
        dist._run(["uv", "venv"], step="创建私有虚拟环境")
    assert "权限" in str(error.value)
    assert "secret" not in str(error.value) and "/Users" not in str(error.value)
    assert "CONDA_PREFIX" not in run.call_args.kwargs["env"]


@pytest.mark.parametrize(
    "output",
    [
        "502 Bad Gateway",
        "503 Service Unavailable",
        "404 Not Found",
        "429 Too Many Requests",
        "certificate verify failed",
        "package was not found in the package registry",
    ],
)
def test_registry_failures_are_retryable(output):
    assert dist._command_failure(output)[1]
