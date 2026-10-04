"""Environment isolation without accounts, OSS uploads, or installing user software."""

from __future__ import annotations

import importlib.util
import json
import plistlib
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from aero import distribution as dist
from aero import release_environment as runtime
from aero.cli import update
from aero.core.config import user_secrets_path
from aero.core.daemon import DaemonManager
from aero.core.llm_providers import get_provider_preset
from aero.core.official_account import platform_api_url, relay_llm_url
from aero.core.runtime_paths import aero_home

ROOT = Path(__file__).resolve().parents[1]
TESTING_PROFILE = {
    "environment": "testing",
    "api_url": "https://api.testing.example.test",
    "llm_url": "https://llm.testing.example.test/v1",
    "download_base": "https://cdn.example.test/download/testing",
}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    for variable in (
        "AERO_ENVIRONMENT",
        "AERO_HOME",
        "AERO_INSTALL_ROOT",
        "AERO_SECRETS_PATH",
        "AERO_OFFICIAL_API_URL",
        "AERO_OFFICIAL_LLM_URL",
        "AERO_DOWNLOAD_BASE",
        "AERO_RELEASE_REQUIRE_CONFIG",
        "AERO_RUNTIME_ROOT",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "PROFILE_PATH", tmp_path / "release_environment.json")
    monkeypatch.setattr(dist, "__file__", str(tmp_path / "distribution.py"))
    return tmp_path


def test_unconfigured_source_keeps_production_defaults(isolated):
    assert runtime.environment_name() == "production"
    assert platform_api_url() == dist.DEFAULT_RUNTIME_PROFILE["api_url"]
    assert relay_llm_url() == dist.DEFAULT_RUNTIME_PROFILE["llm_url"]
    assert aero_home() == isolated / ".aero"
    assert dist.install_root() == isolated / ".aero/client"


def test_testing_wheel_pins_urls_state_and_command(isolated):
    runtime.PROFILE_PATH.write_text(json.dumps(TESTING_PROFILE))
    runtime.configure_runtime()
    assert platform_api_url() == TESTING_PROFILE["api_url"]
    assert relay_llm_url() == TESTING_PROFILE["llm_url"]
    assert get_provider_preset("official").base_url == TESTING_PROFILE["llm_url"]
    assert aero_home() == isolated / ".aero-testing"
    assert user_secrets_path() == isolated / ".aero-testing/secrets.yaml"
    assert dist.install_root() == isolated / ".aero-testing/client"
    assert dist.default_download_base() == TESTING_PROFILE["download_base"]
    launcher = dist.install_launcher(isolated / "bin")
    assert launcher.name == "aero-testing"
    assert "export AERO_ENVIRONMENT=testing" in launcher.read_text()
    assert not (isolated / "bin/aero").exists()
    changed = {**TESTING_PROFILE, "api_url": "https://api.new-testing.example.test"}
    runtime.PROFILE_PATH.write_text(json.dumps(changed))
    assert platform_api_url() == changed["api_url"]  # Startup did not freeze old defaults.


def test_explicit_local_overrides_remain_supported(isolated, monkeypatch):
    runtime.PROFILE_PATH.write_text(json.dumps(TESTING_PROFILE))
    monkeypatch.setenv("AERO_OFFICIAL_API_URL", "http://127.0.0.1:8000/")
    monkeypatch.setenv("AERO_OFFICIAL_LLM_URL", "http://127.0.0.1:8001/v1/")
    monkeypatch.setenv("AERO_HOME", str(isolated / "custom"))
    runtime.configure_runtime()
    assert platform_api_url() == "http://127.0.0.1:8000"
    assert relay_llm_url() == "http://127.0.0.1:8001/v1"
    assert user_secrets_path() == isolated / "custom/secrets.yaml"


def test_mismatched_installed_profile_cannot_be_overridden(isolated, monkeypatch):
    runtime.PROFILE_PATH.write_text(json.dumps(TESTING_PROFILE))
    monkeypatch.setenv("AERO_ENVIRONMENT", "production")
    monkeypatch.setenv("AERO_OFFICIAL_API_URL", "https://api.example.test")
    with pytest.raises(dist.DistributionError, match="不一致"):
        runtime.configure_runtime()
    with pytest.raises(dist.DistributionError, match="不一致"):
        dist.install_root()


def test_testing_source_never_falls_back_to_production(isolated, monkeypatch):
    monkeypatch.setenv("AERO_ENVIRONMENT", "testing")
    with pytest.raises(dist.DistributionError, match="AERO_OFFICIAL_API_URL"):
        runtime.configure_runtime()
    with pytest.raises(dist.DistributionError, match="AERO_DOWNLOAD_BASE"):
        dist.default_download_base()
    assert aero_home() == isolated / ".aero-testing"


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment", "staging"),
        ("api_url", "http://api.example.test"),
        ("api_url", "https://user:secret@api.example.test"),
        ("api_url", "https://api.example.test?token=secret"),
        ("api_url", None),
        ("llm_url", "https://llm.example.test"),
        ("download_base", "https://cdn.example.test/download#fragment"),
    ],
)
def test_invalid_profile_rejected(field, value):
    with pytest.raises(dist.DistributionError):
        dist.validate_runtime_profile({**TESTING_PROFILE, field: value})


def test_cross_environment_update_rejected_before_download(isolated, monkeypatch):
    runtime.PROFILE_PATH.write_text(json.dumps(TESTING_PROFILE))
    legacy = {"version": "0.2.0"}
    with pytest.raises(dist.DistributionError, match="不匹配"):
        dist.assert_release_environment(legacy, "testing")
    dist.assert_release_environment(legacy, "production")
    download = Mock()
    monkeypatch.setattr(dist, "verify_manifest", lambda envelope: envelope["manifest"])
    monkeypatch.setattr(dist, "download", download)
    with pytest.raises(dist.DistributionError, match="不匹配"):
        dist.stage_release({"manifest": legacy}, Path("uv"), TESTING_PROFILE["download_base"])
    download.assert_not_called()
    monkeypatch.setattr(update, "managed_install", lambda: False)
    fetch = Mock(return_value={"manifest": legacy})
    monkeypatch.setattr(update, "fetch_release", fetch)
    with pytest.raises(dist.DistributionError, match="不匹配"):
        update.check_update(force=True)
    fetch.assert_called_once_with(TESTING_PROFILE["download_base"])
    cache = json.loads((dist.install_root() / "update-check.json").read_text())
    assert cache["release"] is None


def test_os_autostart_is_scoped_and_retains_environment(isolated, monkeypatch):
    monkeypatch.setattr(dist, "managed_install", lambda: False)
    prod = DaemonManager(isolated / "project")
    prod_path, _ = prod.autostart_spec("darwin")
    runtime.PROFILE_PATH.write_text(json.dumps(TESTING_PROFILE))
    testing = DaemonManager(isolated / "project")
    testing_path, text = testing.autostart_spec("darwin")
    assert prod_path != testing_path
    config = plistlib.loads(text.encode())
    assert config["EnvironmentVariables"] == {
        "AERO_ENVIRONMENT": "testing",
        "AERO_HOME": str(isolated / ".aero-testing"),
    }
    assert testing.state_dir != prod.state_dir
    _, text = testing.autostart_spec("linux")
    assert 'Environment="AERO_ENVIRONMENT=testing"' in text


def test_release_requires_environment_urls_and_configures_valid_shell(isolated, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "test_prepare", ROOT / "scripts/release/prepare.py"
    )
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    monkeypatch.setenv("AERO_ENVIRONMENT", "testing")
    with pytest.raises(RuntimeError, match="AERO_OFFICIAL_API_URL"):
        prepare.release_profile(dist)
    for field, variable in (
        ("api_url", "AERO_OFFICIAL_API_URL"),
        ("llm_url", "AERO_OFFICIAL_LLM_URL"),
        ("download_base", "AERO_DOWNLOAD_BASE"),
    ):
        monkeypatch.setenv(variable, TESTING_PROFILE[field])
    assert prepare.release_profile(dist) == TESTING_PROFILE
    script = prepare.configured_installer((ROOT / "install.sh").read_text(), TESTING_PROFILE)
    subprocess.run(["sh", "-n"], input=script, text=True, check=True)
    assert "AERO_RELEASE_DEFAULT_AERO_ENVIRONMENT=testing" in script
    assert TESTING_PROFILE["download_base"] in script
