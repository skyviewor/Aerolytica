"""No live account, cloud Agent, package registry, or OSS is used here."""

from __future__ import annotations

import base64
import copy
import io
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aero import distribution as dist
from aero import update_processes
from aero.cli import update


def test_windows_amd64_reports_x64_platform(monkeypatch):
    monkeypatch.setattr(dist.platform, "system", lambda: "Windows")
    monkeypatch.setattr(dist.platform, "machine", lambda: "AMD64")
    assert dist.target_platform() == "windows-x86_64"


@pytest.fixture
def release(monkeypatch, tmp_path):
    monkeypatch.setenv("AERO_INSTALL_ROOT", str(tmp_path / "client"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(update_processes, "legacy_processes", lambda known: [])
    monkeypatch.setattr(dist, "interpreter_version", lambda: "3.12.12")
    key = Ed25519PrivateKey.generate()
    manifest = {
        "schema": 1,
        "distribution": "aerolytica",
        "version": "0.2.0",
        "python": "3.12.12",
        "notes": "Test release",
        "wheel": {"path": "releases/0.2.0/aerolytica-0.2.0-py3-none-any.whl", "sha256": "a" * 64},
        "requirements": {"path": "releases/0.2.0/requirements.txt", "sha256": "b" * 64},
        "platforms": {"macos-aarch64": {"path": "bootstrap/python.tar.gz", "sha256": "c" * 64}},
    }
    keys = {"test": base64.b64encode(key.public_key().public_bytes_raw()).decode()}
    envelope = {
        "manifest": manifest,
        "key_id": "test",
        "signature": base64.b64encode(key.sign(dist.canonical_manifest(manifest))).decode(),
    }
    verify = dist.verify_manifest
    monkeypatch.setattr(dist, "verify_manifest", lambda value: verify(value, keys))
    monkeypatch.setattr(update, "verify_manifest", lambda value: verify(value, keys))
    return envelope


def test_signed_manifest_and_tamper(release):
    assert dist.verify_manifest(release)["version"] == "0.2.0"
    altered = copy.deepcopy(release)
    altered["manifest"]["version"] = "0.9.0"
    with pytest.raises(dist.DistributionError, match="签名"):
        dist.verify_manifest(altered)
    altered = copy.deepcopy(release)
    altered["key_id"] = "untrusted"
    with pytest.raises(dist.DistributionError, match="签名"):
        dist.verify_manifest(altered)


@pytest.mark.parametrize(
    "path",
    ["../wheel", "/wheel", "https://evil.test/wheel", "wheel?token=secret", "a/%2e%2e/b", "a\\b"],
)
def test_manifest_paths_cannot_escape(path):
    with pytest.raises(dist.DistributionError):
        dist.artifact_url("https://example.test/download", path)


@pytest.mark.parametrize(
    "field,value",
    [("schema", 2), ("distribution", "aero"), ("version", "../../oops"), ("platforms", [])],
)
def test_bad_manifest_is_rejected(release, field, value):
    manifest = copy.deepcopy(release["manifest"])
    manifest[field] = value
    with pytest.raises(dist.DistributionError):
        dist.validate_manifest(manifest)


def test_download_hash_failure_does_not_replace_file(monkeypatch, tmp_path):
    class Response(io.BytesIO):
        def geturl(self):
            return "https://example.test/file"

    monkeypatch.setattr(dist.urllib.request, "urlopen", lambda *a, **k: Response(b"corrupt"))
    target = tmp_path / "wheel"
    target.write_bytes(b"previous")
    with pytest.raises(dist.DistributionError, match="校验"):
        dist.download("https://example.test/file", target, sha256="0" * 64)
    assert target.read_bytes() == b"previous"
    assert not target.with_suffix(".part").exists()


def test_download_retries_and_limit(monkeypatch, tmp_path):
    fetch = Mock(side_effect=OSError("offline"))
    monkeypatch.setattr(dist.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(dist.time, "sleep", lambda _: None)
    with pytest.raises(dist.DistributionError, match="下载失败"):
        dist.download("https://example.test/file", tmp_path / "file")
    assert fetch.call_count == 3
    assert all(call.kwargs["timeout"] == 20 for call in fetch.call_args_list)


def test_stage_uses_final_path_and_locked_requirements(release, monkeypatch):
    calls = []
    monkeypatch.setattr(dist, "download", lambda url, path, **kwargs: path.write_bytes(b"fixture"))
    monkeypatch.setattr(dist, "_run", lambda argv, **kwargs: calls.append(argv))
    target = dist.stage_release(
        release, Path("/private/uv"), "https://example.test/download", bootstrap=True
    )
    assert target == dist.install_root() / "versions/0.2.0"
    assert calls[0][-1] == str(target)
    assert "--require-hashes" in calls[1]
    assert "--only-binary" in calls[1]
    marker = json.loads((target / "aero-install.json").read_text())
    assert marker["managed"]
    assert marker["version"] == "0.2.0"
    count = len(calls)
    dist.stage_release(
        release, Path("/private/uv"), "https://example.test/download", bootstrap=True
    )
    assert len(calls) == count + 2  # Only re-run smoke; never rewrite a cached environment.


def test_active_incomplete_environment_is_not_overwritten(release, monkeypatch):
    target = dist.install_root() / "versions/0.2.0"
    target.mkdir(parents=True)
    dist.activate(target)
    run = Mock()
    monkeypatch.setattr(dist, "_run", run)
    with pytest.raises(dist.DistributionError, match="运行中的环境"):
        dist.stage_release(release, Path("/uv"), "https://example.test", bootstrap=True)
    run.assert_not_called()


def test_update_check_success_failure_cache_and_skip(release, monkeypatch):
    monkeypatch.setattr(update, "get_version", lambda: "0.1.0")
    fetch = Mock(return_value=release)
    monkeypatch.setattr(update, "fetch_release", fetch)
    assert update.check_update() == release
    assert update.check_update() == release
    assert fetch.call_count == 1
    update.skip_version("0.2.0")
    assert update.check_update() is None
    assert update.check_update(force=True) == release
    fetch.side_effect = dist.DistributionError("offline")
    with pytest.raises(dist.DistributionError):
        update.check_update(force=True)
    assert update.check_update() is None
    assert fetch.call_count == 3


def test_no_downgrade(release, monkeypatch):
    monkeypatch.setattr(update, "get_version", lambda: "0.3.0")
    monkeypatch.setattr(update, "fetch_release", lambda _: release)
    assert update.check_update(force=True) is None


def test_update_keeps_installed_mirror_without_shell_environment(release, monkeypatch):
    environment = dist.install_root() / "versions/0.1.0"
    dist.write_state(
        environment / "aero-install.json",
        {"managed": True, "base_url": "https://mirror.example.test/download"},
    )
    monkeypatch.setattr(update.sys, "prefix", str(environment))
    monkeypatch.delenv("AERO_DOWNLOAD_BASE", raising=False)
    monkeypatch.setattr(update, "get_version", lambda: "0.1.0")
    fetch = Mock(return_value=release)
    monkeypatch.setattr(update, "fetch_release", fetch)
    assert update.check_update(force=True) == release
    fetch.assert_called_once_with("https://mirror.example.test/download")


def environments():
    old = dist.install_root() / "versions/0.1.0"
    new = dist.install_root() / "versions/0.2.0"
    old.mkdir(parents=True)
    new.mkdir()
    dist.activate(old)
    return old, new


def test_atomic_switch_and_previous(release, monkeypatch):
    old, new = environments()
    monkeypatch.setattr(update, "live_records", lambda **kwargs: [])
    update.switch_environment(new)
    assert dist.current_environment() == new
    assert (dist.install_root() / "previous").resolve() == old
    assert not (dist.install_root() / "updating.json").exists()


def test_busy_task_is_never_stopped(release, monkeypatch):
    old, new = environments()
    record = {
        "pid": 42,
        "kind": "agent",
        "environment": str(old),
        "control": {"agent_id": "fixture"},
        "cwd": "/fixture",
    }
    monkeypatch.setattr(update, "live_records", lambda **kwargs: [record])
    actions = []

    def command(row, action):
        actions.append(action)
        return {"protocol": 1, "update_busy": True}

    monkeypatch.setattr(update, "control", command)
    stop = Mock()
    monkeypatch.setattr(update, "_stop_idle", stop)
    with pytest.raises(dist.DistributionError, match="仍在运行"):
        update.switch_environment(new, wait=0)
    stop.assert_not_called()
    assert actions == ["drain", "status", "resume"]
    assert dist.current_environment() == old


def test_restart_failure_rolls_back_and_restores_cwd(release, monkeypatch):
    old, new = environments()
    record = {
        "pid": 42,
        "kind": "agent",
        "environment": str(old),
        "control": {"agent_id": "fixture"},
        "cwd": "/original/project",
    }
    monkeypatch.setattr(update, "live_records", lambda **kwargs: [record])
    monkeypatch.setattr(update, "control", lambda *args: {"protocol": 1, "update_busy": False})
    monkeypatch.setattr(update, "_stop_idle", lambda row: None)
    restarts = []

    def restart(row, environment):
        restarts.append((row["cwd"], environment))
        if environment == new:
            raise RuntimeError("fixture startup failure")

    monkeypatch.setattr(update, "_restart", restart)
    with pytest.raises(dist.DistributionError, match="恢复旧版本"):
        update.switch_environment(new)
    assert dist.current_environment() == old
    assert restarts == [("/original/project", new), ("/original/project", old)]
    assert not (dist.install_root() / "updating.json").exists()


def test_recovery_failure_leaves_journal(release, monkeypatch):
    old, new = environments()
    record = {
        "pid": 42,
        "kind": "agent",
        "environment": str(old),
        "control": {"agent_id": "fixture"},
        "cwd": "/original",
    }
    monkeypatch.setattr(update, "live_records", lambda **kwargs: [record])
    monkeypatch.setattr(update, "control", lambda *args: {"protocol": 1, "update_busy": False})
    monkeypatch.setattr(update, "_stop_idle", lambda row: None)
    monkeypatch.setattr(update, "_restart", Mock(side_effect=RuntimeError("failed")))
    with pytest.raises(RuntimeError):
        update.switch_environment(new)
    assert (dist.install_root() / "updating.json").exists()
    assert dist.current_environment() == old


def test_inventory_uses_os_lock_not_pid(release, monkeypatch):
    monkeypatch.setattr(update_processes, "managed_install", lambda: True)
    with update_processes.process_record():
        lock = dist.install_root() / "processes" / f"{os.getpid()}.lock"
        assert update_processes.lock_is_live(lock)
        with pytest.raises(dist.DistributionError, match="其他终端"):
            update_processes.live_records()
        assert update_processes.live_records(exclude_pid=os.getpid()) == []
    assert not update_processes.lock_is_live(lock)
    assert update_processes.live_records() == []


def test_new_process_cannot_start_during_update(release, monkeypatch):
    monkeypatch.setattr(update_processes, "managed_install", lambda: True)
    dist.write_state(dist.install_root() / "updating.json", {"token": "private-token"})
    with pytest.raises(dist.DistributionError, match="正在更新"):
        with update_processes.process_record():
            pytest.fail("Must not start")


def test_external_install_is_never_overwritten(release, monkeypatch):
    monkeypatch.setattr(update, "managed_install", lambda: False)
    monkeypatch.setattr(update, "external_update_command", lambda: "pipx upgrade aerolytica")
    stage = Mock()
    monkeypatch.setattr(update, "stage_release", stage)
    with pytest.raises(dist.DistributionError, match="pipx"):
        update.perform_update(release)
    stage.assert_not_called()
