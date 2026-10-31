from __future__ import annotations

# ruff: noqa: F811 -- pytest binds the shared signed-release fixture by name.
import hashlib
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aero import distribution as dist
from aero.cli import pending_update as pending
from aero.cli import update, update_ui
from tests.test_distribution_update import release  # noqa: F401


@pytest.fixture
def prepared(release, monkeypatch):
    monkeypatch.setattr(pending, "managed_install", lambda: True)
    monkeypatch.setattr(pending, "verify_manifest", dist.verify_manifest)
    root = dist.install_root()
    old, new = root / "versions/0.1.2", root / "versions/0.2.0"
    uv = root / "uv"
    dist.private_dir(old)
    dist.private_dir(new)
    uv.touch(mode=0o700)
    dist.write_state(old / "aero-install.json", {"uv": str(uv), "base_url": "https://test.invalid"})
    dist.write_state(
        new / "aero-install.json",
        {
            "managed": True,
            "version": "0.2.0",
            "manifest_hash": hashlib.sha256(
                dist.canonical_manifest(release["manifest"])
            ).hexdigest(),
        },
    )
    monkeypatch.setattr(sys, "prefix", str(old))
    monkeypatch.setattr(pending, "current_environment", lambda: old)
    monkeypatch.setattr(pending, "get_version", lambda: "0.1.2")
    monkeypatch.setattr(update, "check_update", Mock(return_value=release))
    monkeypatch.setattr(pending, "stage_release", Mock(return_value=new))
    monkeypatch.setattr(pending.subprocess, "Popen", Mock())
    monkeypatch.setattr(pending, "smoke", Mock())
    monkeypatch.setattr(update, "switch_environment", Mock())
    return root, old, new


def finish_job():
    job = pending._read("prepare-update.json")
    pending.run_preparer(job["job_id"])


def test_prepare_is_detached_deduplicated_and_never_activates(prepared):
    root, old, new = prepared
    assert pending.prepare_update()["status"] == "preparing"
    assert pending.prepare_update()["status"] == "preparing"
    assert pending.subprocess.Popen.call_count == 1
    assert pending.subprocess.Popen.call_args.kwargs["start_new_session"] is True
    assert not (root / "updating.json").exists()
    finish_job()
    assert pending.stage_release.call_count == 1
    assert update.switch_environment.call_count == 0
    assert pending.current_environment() == old
    status = pending.update_status()
    assert status["status"] == "ready" and status["running_version"] == "0.1.2"
    assert "environment" not in status and "release" not in status
    assert pending.prepare_update()["status"] == "ready"
    assert json.loads((root / "pending-update.json").read_text())["environment"] == str(new)


def test_manual_apply_smokes_then_safe_switch_and_removes_pending(prepared):
    root, _, new = prepared
    pending.prepare_update()
    finish_job()
    assert pending.apply_pending(exclude_pid=123)
    pending.smoke.assert_called_once_with(new, "0.2.0")
    update.switch_environment.assert_called_once_with(new, exclude_pid=123, wait=0)
    assert not (root / "pending-update.json").exists()


def test_busy_peers_or_failed_start_leave_staged_record_and_old_runtime(prepared):
    root, old, _ = prepared
    pending.prepare_update()
    finish_job()
    update.switch_environment.side_effect = dist.DistributionError("任务仍在运行")
    with pytest.raises(dist.DistributionError, match="任务"):
        pending.apply_pending()
    assert (root / "pending-update.json").exists() and pending.current_environment() == old


def test_tampered_pending_or_marker_is_not_applied(prepared):
    root, _, new = prepared
    pending.prepare_update()
    finish_job()
    marker = pending._read("pending-update.json")
    marker["release"]["manifest"]["version"] = "9.0.0"
    dist.write_state(root / "pending-update.json", marker)
    with pytest.raises(dist.DistributionError, match="签名"):
        pending.apply_pending()
    update.switch_environment.assert_not_called()
    assert new.exists()


def test_prepare_failure_is_bounded_and_retryable_without_activation(prepared):
    pending.stage_release.side_effect = dist.DistributionError("签名或下载错误")
    pending.prepare_update()
    finish_job()
    assert pending.update_status()["status"] == "error"
    update.switch_environment.assert_not_called()
    assert pending.prepare_update()["status"] == "preparing"
    assert pending.subprocess.Popen.call_count == 2


def test_stale_job_can_be_replaced_and_old_worker_cannot_write(prepared):
    root, _, _ = prepared
    pending.prepare_update()
    old = pending._read("prepare-update.json")
    dist.write_state(root / "prepare-update.json", {**old, "started_at": time.time() - 60})
    assert pending.update_status()["status"] == "interrupted"
    assert pending.prepare_update()["status"] == "preparing"
    pending.run_preparer(old["job_id"])
    pending.stage_release.assert_not_called()


def test_source_or_pip_install_never_spawns_or_mutates(prepared, monkeypatch):
    monkeypatch.setattr(pending, "managed_install", lambda: False)
    monkeypatch.setattr(update, "external_update_command", lambda: "pip install -U aerolytica")
    assert pending.prepare_update()["status"] == "external"
    assert not pending.apply_pending()
    pending.subprocess.Popen.assert_not_called()
    update.check_update.assert_not_called()


async def test_restart_saves_session_and_only_exits_after_manual_apply(prepared):
    pending.prepare_update()
    finish_job()
    app = SimpleNamespace(
        screen_stack=[object()], notify=Mock(), _save_session_on_exit=Mock(), exit=Mock()
    )
    await update_ui.request_restart(app)
    app._save_session_on_exit.assert_called_once()
    app.exit.assert_called_once()
    assert app._restart_for_update is True


@pytest.mark.parametrize(
    "command,internal",
    [(["chat"], False), (["chat"], True), (["agent", "run"], False), (["serve"], False)],
)
def test_only_human_chat_start_checks_pending_activation(prepared, monkeypatch, command, internal):
    from aero import release_environment
    from aero.cli import entrypoint, main

    apply = Mock(return_value=False)
    monkeypatch.setattr(pending, "apply_pending", apply)
    monkeypatch.setattr(main, "main", Mock())
    monkeypatch.setattr(release_environment, "configure_runtime", lambda: None)
    monkeypatch.setattr(sys, "argv", ["aero", *command])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    if command == ["serve"]:
        monkeypatch.setenv("AERO_ENABLE_WEB_PREVIEW", "1")
    if internal:
        monkeypatch.setenv("AERO_UPDATE_RESTART_TOKEN", "internal")
    else:
        monkeypatch.delenv("AERO_UPDATE_RESTART_TOKEN", raising=False)
    entrypoint.main()
    assert apply.call_count == int(command == ["chat"] and not internal)


async def test_shared_chat_restart_button_is_compact_and_only_shown_when_ready(monkeypatch):
    from textual.widgets import Button

    from aero.cli.main import ChatTuiApp

    status = {"status": "idle"}
    monkeypatch.setattr(pending, "update_status", lambda: status)
    app = ChatTuiApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await update_ui.refresh_pending(app)
        button = app.query_one("#restart-update", Button)
        assert not button.display
        status.update(status="ready", version="0.2.0", message="准备完成，请手动重启")
        await update_ui.refresh_pending(app)
        await pilot.pause()
        assert button.display and button.region.height == 1
        assert button.region.width == 18
        assert not getattr(app, "_restart_for_update", False)


def test_failed_exec_uses_idle_only_rollback_before_relaunch(prepared, monkeypatch):
    from aero.cli import entrypoint

    rollback = Mock()
    launch = Mock(side_effect=[OSError("cannot execute"), None])
    monkeypatch.setattr(update, "rollback", rollback)
    monkeypatch.setattr(entrypoint.os, "execv", launch)
    entrypoint._restart_current(["chat", "--continue"])
    rollback.assert_called_once_with(wait=0)
    assert launch.call_count == 2
