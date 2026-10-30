"""The downloaded bootstrap stages first and never bypasses update handoff."""

from __future__ import annotations

import importlib.util
import json
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aero.distribution import DistributionError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bootstrap(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "reinstall_bootstrap", ROOT / "scripts/release/bootstrap.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    previous = {"managed": True, "version": "0.1.2", "runtime": {"environment": "testing"}}
    marker = old / "aero-install.json"
    marker.write_text(json.dumps(previous))
    manifest = {"version": "0.1.2", "revision": "a" * 40, "runtime": {"environment": "testing"}}
    path = tmp_path / "release.json"
    path.write_text(json.dumps({"manifest": manifest}))
    wheel = tmp_path / "wheel.zip"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("aero/distribution.py", "# isolated fixture")
    lock = {"held": False}

    @contextmanager
    def install_lock():
        lock["held"] = True
        try:
            yield
        finally:
            lock["held"] = False

    installed = SimpleNamespace(
        DistributionError=DistributionError,
        install_lock=install_lock,
        stage_release=Mock(return_value=new),
        current_environment=lambda: old,
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

    def run(command, **kwargs):
        assert not lock["held"], "The updater must be able to reacquire the install lock"
        assert command == [str(new / "bin/aero"), "update", "--reinstall", "--yes"]
        return SimpleNamespace(returncode=0)

    runner = Mock(side_effect=run)
    module.subprocess = SimpleNamespace(run=runner)
    monkeypatch.setattr(
        module.sys,
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
    monkeypatch.delenv("AERO_REINSTALL", raising=False)
    return module, installed, runner, manifest, path, marker


def test_existing_install_requires_explicit_reinstall(bootstrap):
    module, installed, runner, _, _, _ = bootstrap
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 1
    installed.activate.assert_not_called()
    runner.assert_not_called()


def test_confirmed_testing_reinstall_uses_installed_updater_after_unlock(bootstrap, monkeypatch):
    module, installed, runner, _, _, _ = bootstrap
    monkeypatch.setenv("AERO_REINSTALL", "1")
    module.main()
    installed.stage_release.assert_called_once()
    installed.activate.assert_not_called()
    runner.assert_called_once()


@pytest.mark.parametrize("change", ["production", "other_version", "production_current"])
def test_reinstall_flag_cannot_replace_production_or_another_version(
    bootstrap, monkeypatch, change
):
    module, installed, runner, manifest, path, marker = bootstrap
    monkeypatch.setenv("AERO_REINSTALL", "1")
    if change == "production":
        manifest["runtime"]["environment"] = "production"
    elif change == "other_version":
        manifest["version"] = "0.1.3"
    else:
        previous = json.loads(marker.read_text())
        previous["runtime"]["environment"] = "production"
        marker.write_text(json.dumps(previous))
    path.write_text(json.dumps({"manifest": manifest}))
    with pytest.raises(SystemExit):
        module.main()
    installed.activate.assert_not_called()
    runner.assert_not_called()
