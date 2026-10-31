"""Version replacement uses the installed controller and the exact signed target."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aero import distribution as dist
from aero import update_processes
from aero.cli import update

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bootstrap(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "reinstall_bootstrap", ROOT / "scripts/release/bootstrap.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "client"
    old = root / "versions/0.1.2-old"
    old.mkdir(parents=True)
    previous = {"managed": True, "version": "0.1.2", "runtime": {"environment": "testing"}}
    marker = old / "aero-install.json"
    marker.write_text(json.dumps(previous))
    manifest = {
        "version": "0.1.2",
        "revision": "a" * 40,
        "python": "3.12.12",
        "runtime": {"environment": "testing"},
    }
    path = tmp_path / "release.json"
    path.write_text(json.dumps({"manifest": manifest}))
    wheel = tmp_path / "wheel.zip"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("aero/distribution.py", "# isolated fixture")
    lock, current = {"held": False}, {"env": old}

    @contextmanager
    def install_lock():
        assert not lock["held"]
        lock["held"] = True
        try:
            yield
        finally:
            lock["held"] = False

    def stage(envelope, *args, **kwargs):
        assert lock["held"]
        data = envelope["manifest"]
        name = data["version"] + ("-" + data["revision"] if data.get("revision") else "")
        target = root / "versions" / name
        target.mkdir(exist_ok=True)
        (target / "aero-install.json").write_text(
            json.dumps(
                {
                    "managed": True,
                    "manifest_hash": hashlib.sha256(dist.canonical_manifest(data)).hexdigest(),
                }
            )
        )
        return target

    def assert_environment(data, expected):
        if data["runtime"]["environment"] != expected:
            raise dist.DistributionError("发行清单环境不符")

    installed = SimpleNamespace(
        DistributionError=dist.DistributionError,
        install_lock=install_lock,
        stage_release=Mock(side_effect=stage),
        current_environment=lambda: current["env"],
        install_root=lambda: root,
        validate_manifest=lambda data: data,
        assert_release_environment=assert_environment,
        interpreter_version=lambda: "3.12.12",
        install_launcher=Mock(),
        activate=Mock(),
    )
    module.importlib = SimpleNamespace(
        util=SimpleNamespace(
            spec_from_file_location=lambda *args: SimpleNamespace(
                loader=SimpleNamespace(exec_module=lambda _: None)
            ),
            module_from_spec=lambda _: installed,
        )
    )
    monkeypatch.setattr(dist, "install_lock", install_lock)
    monkeypatch.setattr(dist, "install_root", lambda: root)
    monkeypatch.setattr(dist, "current_environment", lambda: current["env"])
    monkeypatch.setattr(dist, "assert_release_environment", assert_environment)
    monkeypatch.setattr(dist, "verify_manifest", lambda data: data["manifest"])
    monkeypatch.setattr(dist, "smoke", Mock())
    monkeypatch.setattr(update_processes, "lock_is_live", lambda _: False)
    switch = Mock()
    monkeypatch.setattr(update, "switch_environment", switch)

    def run(command, **kwargs):
        assert not lock["held"], "The installed controller reacquires its own install lock"
        assert command[:3] == [str(old / "bin/python"), "-c", module.HANDOFF]
        with monkeypatch.context() as local:
            local.setattr(sys, "argv", ["-c", *command[3:]])
            try:
                exec(module.HANDOFF, {})
            except SystemExit as error:
                return SimpleNamespace(returncode=error.code)
        return SimpleNamespace(returncode=0)

    runner = Mock(side_effect=run)
    module.subprocess = SimpleNamespace(run=runner)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap.py",
            "--base",
            "https://testing.example.test/download",
            "--uv",
            "/uv",
            "--manifest",
            str(path),
            "--wheel",
            str(wheel),
        ],
    )
    for name in (
        "AERO_VERSION",
        "AERO_INSTALL_REVISION",
        "AERO_ALLOW_DOWNGRADE",
        "AERO_REINSTALL",
        "AERO_RELEASE_MANIFEST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AERO_ENVIRONMENT", "testing")
    return SimpleNamespace(
        module=module,
        installed=installed,
        runner=runner,
        manifest=manifest,
        path=path,
        marker=marker,
        switch=switch,
        current=current,
        root=root,
    )


def save(b):
    b.path.write_text(json.dumps({"manifest": b.manifest}))


def test_existing_install_requires_explicit_reinstall(bootstrap):
    b = bootstrap
    with pytest.raises(SystemExit) as error:
        b.module.main()
    assert error.value.code == 1
    b.installed.activate.assert_not_called()
    b.installed.stage_release.assert_not_called()
    b.switch.assert_not_called()


def test_confirmed_testing_reinstall_uses_installed_updater_after_unlock(bootstrap, monkeypatch):
    b = bootstrap
    monkeypatch.setenv("AERO_REINSTALL", "1")
    b.module.main()
    b.installed.stage_release.assert_called_once()
    b.installed.activate.assert_not_called()
    assert [call.args[0][-1] for call in b.runner.call_args_list] == ["check", "apply"]
    assert b.switch.call_args.kwargs == {"wait": 0}


@pytest.mark.parametrize("change", ["production", "other_version", "production_current"])
def test_reinstall_flag_cannot_replace_production_or_another_version(
    bootstrap, monkeypatch, change
):
    b = bootstrap
    monkeypatch.setenv("AERO_REINSTALL", "1")
    if change == "production":
        b.manifest["runtime"]["environment"] = "production"
    elif change == "other_version":
        b.manifest["version"] = "0.1.3"
    else:
        previous = json.loads(b.marker.read_text())
        previous["runtime"]["environment"] = "production"
        b.marker.write_text(json.dumps(previous))
    save(b)
    with pytest.raises(SystemExit):
        b.module.main()
    b.installed.activate.assert_not_called()
    b.installed.stage_release.assert_not_called()
    b.switch.assert_not_called()


@pytest.mark.parametrize("allowed", [False, True])
def test_downgrade_requires_explicit_version_and_consent(bootstrap, monkeypatch, allowed):
    b = bootstrap
    b.manifest["version"] = "0.1.1"
    b.manifest.pop("revision")
    save(b)
    monkeypatch.setenv("AERO_VERSION", "0.1.1")
    if allowed:
        monkeypatch.setenv("AERO_ALLOW_DOWNGRADE", "1")
        b.module.main()
        b.switch.assert_called_once()
        assert b.switch.call_args.args[0].name == "0.1.1"
    else:
        with pytest.raises(SystemExit):
            b.module.main()
        b.installed.stage_release.assert_not_called()
        b.switch.assert_not_called()


def test_requested_revision_is_exact_and_does_not_refetch_stable(bootstrap, monkeypatch):
    b = bootstrap
    monkeypatch.setenv("AERO_VERSION", "0.1.2")
    monkeypatch.setenv("AERO_INSTALL_REVISION", b.manifest["revision"])
    b.module.main()
    assert b.switch.call_args.args[0].name == "0.1.2-" + "a" * 40
    assert all("fetch_release" not in call.args[0][2] for call in b.runner.call_args_list)


def test_fresh_install_activates_after_staging_without_handoff(bootstrap, monkeypatch):
    b = bootstrap
    b.current["env"] = None
    monkeypatch.setenv("AERO_VERSION", "0.1.2")
    b.module.main()
    b.installed.stage_release.assert_called_once()
    b.installed.activate.assert_called_once()
    b.runner.assert_not_called()


def test_busy_handoff_leaves_pending_update_and_old_install(bootstrap, monkeypatch):
    b = bootstrap
    monkeypatch.setenv("AERO_REINSTALL", "1")
    pending = b.root / "pending-update.json"
    pending.write_text('{"previously_prepared": true}')
    b.switch.side_effect = dist.DistributionError("任务繁忙，未强杀")
    with pytest.raises(SystemExit):
        b.module.main()
    assert pending.exists()
    b.installed.activate.assert_not_called()


def test_explicit_pin_clears_pending_activation_but_keeps_prepared_files(bootstrap, monkeypatch):
    b = bootstrap
    monkeypatch.setenv("AERO_REINSTALL", "1")
    pending = b.root / "pending-update.json"
    pending.write_text('{"previously_prepared": true}')
    staged = b.root / "versions/0.9.0"
    staged.mkdir()
    b.module.main()
    assert not pending.exists() and staged.exists()


def test_installed_trust_store_rejects_bad_signature_before_staging(bootstrap, monkeypatch):
    b = bootstrap
    monkeypatch.setenv("AERO_REINSTALL", "1")
    monkeypatch.setattr(
        dist, "verify_manifest", Mock(side_effect=dist.DistributionError("签名错误"))
    )
    with pytest.raises(SystemExit):
        b.module.main()
    b.installed.stage_release.assert_not_called()
    b.switch.assert_not_called()


def test_historical_python_is_isolated_and_pinned_to_its_hash(bootstrap, monkeypatch):
    b = bootstrap
    monkeypatch.setattr(dist, "target_platform", lambda: "macos-aarch64")
    manifest = {
        "python": "3.12.10",
        "platforms": {
            "macos-aarch64": {"path": "bootstrap/python/historical.tar.gz", "sha256": "c" * 64}
        },
    }

    def download(url, archive, **kwargs):
        with tarfile.open(archive, "w:gz") as bundle:
            item = tarfile.TarInfo("python/bin/python3.12")
            item.size = 4
            bundle.addfile(item, io.BytesIO(b"mock"))

    downloader = Mock(side_effect=download)
    monkeypatch.setattr(dist, "download", downloader)
    target = b.module.selected_interpreter(dist, manifest, "https://testing.example.test/download")
    assert "3.12.10-macos-aarch64-" + "c" * 16 in str(target)
    assert target.read_bytes() == b"mock"
    assert b.marker.exists()
    assert downloader.call_args.kwargs == {"sha256": "c" * 64}
    b.module.selected_interpreter(dist, manifest, "https://testing.example.test/download")
    downloader.assert_called_once()


def test_wrong_historical_interpreter_stops_instead_of_reexec_loop(bootstrap, monkeypatch):
    b = bootstrap
    b.installed.interpreter_version = lambda: "3.12.10"
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--interpreter-ready"])
    select = Mock()
    monkeypatch.setattr(b.module, "selected_interpreter", select)
    with pytest.raises(SystemExit):
        b.module.main()
    select.assert_not_called()
    b.installed.stage_release.assert_not_called()
