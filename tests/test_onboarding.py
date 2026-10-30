"""Environment setup is independent of accounts, models, and the chat UI."""

from __future__ import annotations

import builtins
from unittest.mock import Mock

import pytest

from aero.cli import entrypoint, onboarding


@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize(
    "args,full,yes,china",
    [
        ([], False, False, False),
        (["--yes"], False, True, False),
        (["--runtime-only", "--yes"], False, True, False),
        (["--full", "--china-mirror", "-y"], True, True, True),
    ],
)
def test_setup_only_prepares_runtime(monkeypatch, tmp_path, interactive, args, full, yes, china):
    monkeypatch.setattr("sys.stdin.isatty", lambda: interactive)
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    secrets = tmp_path / "secrets.yaml"
    existing = b"official_account: {user_id: test-user}\nllm: {model: existing-model}\n"
    secrets.write_bytes(existing)
    runtime = Mock(return_value=True)
    monkeypatch.setattr("aero.cli.init_runtime.setup_runtime", runtime)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith(
            (
                "textual",
                "aero.cli.main",
                "aero.cli.setup_wizard",
                "aero.cli.device_login",
                "aero.core.official_account",
            )
        )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert onboarding.main(args) == 0
    runtime.assert_called_once_with(full=full, assume_yes=yes, china_mirror=china)
    assert secrets.read_bytes() == existing
    assert not (tmp_path / "aero.yaml").exists()


def test_logged_out_setup_succeeds_without_creating_credentials(monkeypatch, tmp_path, capsys):
    path = tmp_path / "secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(path))
    monkeypatch.setattr("aero.cli.init_runtime.setup_runtime", Mock(return_value=True))
    assert onboarding.main(["--yes"]) == 0
    assert not path.exists()
    output = capsys.readouterr().out
    assert "setup 不要求登录" in output
    assert "aero login" in output


def test_runtime_failure_does_not_report_setup_success(monkeypatch, capsys):
    runtime = Mock(return_value=False)
    monkeypatch.setattr("aero.cli.init_runtime.setup_runtime", runtime)
    assert onboarding.main([]) == 1
    assert "已就绪" not in capsys.readouterr().out
    runtime.assert_called_once()


@pytest.mark.parametrize(
    "environment,command", [("production", "aero"), ("testing", "aero-testing")]
)
def test_setup_hints_match_the_installation_environment(monkeypatch, capsys, environment, command):
    monkeypatch.setenv("AERO_ENVIRONMENT", environment)
    monkeypatch.setattr("aero.cli.init_runtime.setup_runtime", Mock(return_value=True))
    assert onboarding.main(["--yes"]) == 0
    output = capsys.readouterr().out
    assert f"{command} init" in output and f"{command} chat" in output
    assert f"{command} login" in output


def test_setup_entrypoint_remains_lightweight_and_forwards_flags(monkeypatch):
    monkeypatch.setattr("aero.release_environment.configure_runtime", lambda: None)
    monkeypatch.setattr("sys.argv", ["aero-testing", "setup", "--yes", "--full"])
    setup = Mock(return_value=0)
    monkeypatch.setattr(onboarding, "main", setup)
    with pytest.raises(SystemExit) as exited:
        entrypoint.main()
    assert exited.value.code == 0
    setup.assert_called_once_with(["--yes", "--full"])
