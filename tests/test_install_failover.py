"""Installer diagnostics and failover use simulated registries only."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from aero import distribution as dist


def test_first_install_lock_works_without_aero_or_site_packages(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import importlib.util,os,sys; "
            "os.environ['AERO_INSTALL_ROOT']=sys.argv[2]; "
            "s=importlib.util.spec_from_file_location('bootstrap_distribution',sys.argv[1]); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            'exec(\'with m.install_lock():\\n assert not any(n == \\"aero\\" or '
            'n.startswith(\\"aero.\\") for n in sys.modules)\'); '
            "print('bootstrap lock OK')",
            str(Path(dist.__file__).resolve()),
            str(tmp_path / "client"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "bootstrap lock OK" in result.stdout


@pytest.fixture(autouse=True)
def isolated_indexes(monkeypatch):
    for name in (
        *dist.DEPENDENCY_SOURCE_VARIABLES,
        "UV_NO_INDEX",
        "UV_OFFLINE",
        "UV_INDEX_PRIVATE_USERNAME",
        "UV_INDEX_PRIVATE_PASSWORD",
        "UV_KEYRING_PROVIDER",
        "UV_INSECURE_HOST",
        "UV_INDEX_STRATEGY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AERO_NETWORK_REGION", "mainland_china")


def test_forbidden_mirror_is_skipped_before_install_without_error_noise(monkeypatch, capsys):
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 1, "", "403 Forbidden https://private:secret@host/"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(dist.subprocess, "run", run)
    dist._sync_dependencies(Path("/uv"), Path("/python"), Path("/locked.txt"))
    assert run.call_count == 3
    first, second, install = run.call_args_list
    assert first.kwargs["env"]["UV_DEFAULT_INDEX"] == "https://mirrors.aliyun.com/pypi/simple"
    assert second.kwargs["env"]["UV_DEFAULT_INDEX"] == (
        "https://mirrors.cloud.tencent.com/pypi/simple"
    )
    assert install.kwargs["env"] == second.kwargs["env"]
    assert first.args == second.args
    assert second.args[0] == [*install.args[0], "--dry-run", "--refresh"]
    command = install.args[0]
    assert "--require-hashes" in command
    assert "--only-binary" in command
    assert "--no-config" in command
    assert "--allow-insecure-host" not in command
    assert first.kwargs["timeout"] == dist.DEPENDENCY_PROBE_TIMEOUT
    assert install.kwargs["timeout"] == 180
    captured = capsys.readouterr()
    assert "检查依赖源可用性" in captured.out and "腾讯云" in captured.out
    assert not captured.err
    assert all(value not in captured.out for value in ("403", "失败", "secret", "private"))


@pytest.mark.parametrize("failure", ["Hash mismatch for package", "No space left on device"])
@pytest.mark.parametrize("during_probe", [True, False])
def test_integrity_and_disk_errors_do_not_switch_sources(monkeypatch, failure, during_probe):
    results = [] if during_probe else [subprocess.CompletedProcess([], 0, "", "")]
    run = Mock(side_effect=[*results, subprocess.CompletedProcess([], 1, "", failure)])
    monkeypatch.setattr(dist.subprocess, "run", run)
    with pytest.raises(dist.InstallCommandError):
        dist._sync_dependencies(Path("/uv"), Path("/python"), Path("/locked.txt"))
    assert run.call_count == (1 if during_probe else 2)


@pytest.mark.parametrize("name", dist.DEPENDENCY_SOURCE_VARIABLES)
@pytest.mark.parametrize(
    "failure",
    [
        "403 Forbidden",
        "No solution found when resolving dependencies",
        "invalid value 'broken' for '--index-url': relative URL without a base",
    ],
)
def test_unavailable_environment_registry_falls_back_with_isolated_credentials(
    monkeypatch, capsys, name, failure
):
    monkeypatch.setenv(name, "https://user:secret@private.example/simple")
    monkeypatch.setenv("UV_INDEX_PRIVATE_USERNAME", "private-user")
    monkeypatch.setenv("UV_INDEX_PRIVATE_PASSWORD", "private-password")
    monkeypatch.setenv("UV_KEYRING_PROVIDER", "subprocess")
    monkeypatch.setenv("UV_INSECURE_HOST", "private.example")
    monkeypatch.setenv("UV_INDEX_STRATEGY", "unsafe-best-match")
    original = dict(dist.os.environ)
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 1, "", failure),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(dist.subprocess, "run", run)
    dist._sync_dependencies(Path("/uv"), Path("/python"), Path("/locked.txt"))
    assert run.call_count == 3
    assert run.call_args_list[0].kwargs["env"][name] == (
        "https://user:secret@private.example/simple"
    )
    for call in run.call_args_list[1:]:
        environment = call.kwargs["env"]
        assert environment["UV_DEFAULT_INDEX"] == "https://mirrors.aliyun.com/pypi/simple"
        assert all(
            variable not in environment
            for variable in dist.DEPENDENCY_SOURCE_VARIABLES
            if variable != "UV_DEFAULT_INDEX"
        )
        assert "UV_INDEX_PRIVATE_USERNAME" not in environment
        assert "UV_INDEX_PRIVATE_PASSWORD" not in environment
        assert "UV_INSECURE_HOST" not in environment
        assert "UV_INDEX_STRATEGY" not in environment
        assert environment["UV_KEYRING_PROVIDER"] == "disabled"
    assert dict(dist.os.environ) == original
    captured = capsys.readouterr()
    assert not captured.err
    assert all(value not in captured.out for value in ("secret", "private", "403", "失败"))


def test_working_environment_sources_are_preferred_and_not_rewritten(monkeypatch, capsys):
    monkeypatch.setenv("UV_INDEX", "private=https://private.example/simple")
    monkeypatch.setenv("UV_DEFAULT_INDEX", "https://user:secret@custom.example/simple")
    monkeypatch.setenv("UV_INDEX_PRIVATE_PASSWORD", "private-password")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(dist.subprocess, "run", run)
    dist._sync_dependencies(Path("/uv"), Path("/python"), Path("/locked.txt"))
    assert run.call_count == 2
    for call in run.call_args_list:
        assert call.kwargs["env"]["UV_INDEX"] == "private=https://private.example/simple"
        assert call.kwargs["env"]["UV_INDEX_PRIVATE_PASSWORD"] == "private-password"
        assert call.kwargs["env"]["UV_DEFAULT_INDEX"] == (
            "https://user:secret@custom.example/simple"
        )
    output = capsys.readouterr().out
    assert "环境变量指定的源" in output
    assert "secret" not in output and "private" not in output


def test_all_preflight_checks_fail_without_attempting_install(monkeypatch):
    run = Mock(side_effect=subprocess.TimeoutExpired(["uv", "private-token"], 180))
    monkeypatch.setattr(dist.subprocess, "run", run)
    with pytest.raises(dist.InstallCommandError, match="没有可用的依赖源") as error:
        dist._sync_dependencies(Path("/uv"), Path("/python"), Path("/locked.txt"))
    assert run.call_count == 5
    assert all("--dry-run" in call.args[0] for call in run.call_args_list)
    assert all(
        call.kwargs["timeout"] == dist.DEPENDENCY_PROBE_TIMEOUT for call in run.call_args_list
    )
    assert error.value.retryable
    assert "private-token" not in str(error.value)


def test_connection_lost_after_preflight_uses_next_source_without_error_noise(monkeypatch, capsys):
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", "503 Service Unavailable secret"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(dist.subprocess, "run", run)
    dist._sync_dependencies(Path("/uv"), Path("/python"), Path("/locked.txt"))
    assert run.call_count == 4
    assert ["--dry-run" in call.args[0] for call in run.call_args_list] == [
        True,
        False,
        True,
        False,
    ]
    captured = capsys.readouterr()
    assert "重新选择可用源" in captured.out
    assert "失败" not in captured.out and "secret" not in captured.out and "503" not in captured.out
    assert not captured.err


def test_all_install_attempts_are_bounded_and_final_error_is_redacted(monkeypatch):
    def execute(command, **kwargs):
        if "--dry-run" in command:
            return subprocess.CompletedProcess([], 0, "", "")
        raise subprocess.TimeoutExpired(["uv", "private-token"], 180)

    run = Mock(side_effect=execute)
    monkeypatch.setattr(dist.subprocess, "run", run)
    with pytest.raises(dist.InstallCommandError, match="安装锁定依赖超时") as error:
        dist._sync_dependencies(Path("/uv"), Path("/python"), Path("/locked.txt"))
    assert run.call_count == 10
    assert "private-token" not in str(error.value)


@pytest.mark.parametrize("variable", ["UV_OFFLINE", "UV_NO_INDEX"])
def test_explicit_offline_operation_does_not_contact_public_mirrors(monkeypatch, variable):
    monkeypatch.setenv(variable, "true")
    assert dist._dependency_indexes() == [None]
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(dist.subprocess, "run", run)
    dist._sync_dependencies(Path("/uv"), Path("/python"), Path("/locked.txt"))
    assert run.call_count == 2
    assert all("--refresh" not in call.args[0] for call in run.call_args_list)
    assert all(call.kwargs["env"][variable] == "true" for call in run.call_args_list)


def test_mainland_priority_includes_five_distinct_sources():
    assert dist._dependency_indexes() == [
        "https://mirrors.aliyun.com/pypi/simple",
        "https://mirrors.cloud.tencent.com/pypi/simple",
        "https://repo.huaweicloud.com/repository/pypi/simple",
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "https://pypi.org/simple",
    ]


def test_non_mainland_prefers_official_registry(monkeypatch):
    monkeypatch.setenv("AERO_NETWORK_REGION", "global")
    assert dist._dependency_indexes() == [
        "https://pypi.org/simple",
        "https://mirrors.aliyun.com/pypi/simple",
        "https://mirrors.cloud.tencent.com/pypi/simple",
        "https://repo.huaweicloud.com/repository/pypi/simple",
        "https://pypi.tuna.tsinghua.edu.cn/simple",
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
        "No solution found when resolving dependencies",
        "invalid URL for index",
    ],
)
def test_registry_failures_are_retryable(output):
    assert dist._command_failure(output)[1]
