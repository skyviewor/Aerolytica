from __future__ import annotations

import base64
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aero import i18n
from aero import version as runtime_version
from aero.distribution import verify_manifest

ROOT = Path(__file__).resolve().parents[1]


def test_real_version_is_used_by_all_language_titles(monkeypatch):
    monkeypatch.setattr(runtime_version, "version", lambda name: "1.2.34")
    assert runtime_version.get_version() == "1.2.34"
    monkeypatch.setattr(i18n, "get_version", runtime_version.get_version)
    for language, _ in i18n.language_options():
        assert "v1.2.34" in i18n.t("app.title", language)
        assert "{version}" not in i18n.t("app.title", language)


def test_package_name_and_entrypoint():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert metadata["project"]["name"] == "aerolytica"
    assert metadata["project"]["requires-python"] == ">=3.12"
    assert metadata["project"]["scripts"]["aero"] == "aero.cli.entrypoint:main"
    assert metadata["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/aero"]


def test_release_order_secret_scope_and_immutable_actions():
    workflow = yaml.load(
        (ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert workflow["on"]["release"]["types"] == ["published"]
    assert workflow["on"]["workflow_dispatch"]["inputs"]["environment"]["default"] == "testing"
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    jobs = workflow["jobs"]
    assert set(jobs) == {"build", "stage-oss", "install-smoke", "promote-oss"}
    assert workflow["permissions"] == {"contents": "read"}
    assert jobs["stage-oss"]["needs"] == "build"
    assert jobs["install-smoke"]["needs"] == "stage-oss"
    assert jobs["promote-oss"]["needs"] == "install-smoke"
    for job in jobs.values():
        assert job["environment"] == (
            "${{ github.event_name == 'release' && 'production' || inputs.environment }}"
        )
        assert "id-token" not in job.get("permissions", {})
        assert "OSS_ACCESS_KEY_SECRET" not in job.get("env", {})
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
                assert "pypi-publish" not in step["uses"]
            assert "check_pypi.py" not in step.get("run", "")
            if "OSS_ACCESS_KEY_SECRET" in step.get("env", {}):
                assert "scripts/release/publish_oss.py" in step["run"]
            assert "continue-on-error" not in step


def test_testpypi_is_paused_unless_explicitly_enabled():
    workflow = yaml.load(
        (ROOT / ".github/workflows/testpypi.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["jobs"]["rehearsal"]["if"] == ("${{ vars.ENABLE_TESTPYPI_PUBLISH == 'true' }}")


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required for a real release build")
@pytest.mark.parametrize("environment", ["production", "testing"])
def test_real_signed_wheel_sdist_and_manifest_are_consistent(monkeypatch, tmp_path, environment):
    key = Ed25519PrivateKey.generate()
    seed = base64.b64encode(key.private_bytes_raw()).decode()
    monkeypatch.setenv("RELEASE_SIGNING_KEY", seed)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1728000000")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("AERO_ENVIRONMENT", environment)
    for variable in ("AERO_OFFICIAL_API_URL", "AERO_OFFICIAL_LLM_URL", "AERO_DOWNLOAD_BASE"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.delenv("AERO_RELEASE_REQUIRE_CONFIG", raising=False)
    if environment == "testing":
        monkeypatch.setenv("AERO_OFFICIAL_API_URL", "https://api.testing.example.test")
        monkeypatch.setenv("AERO_OFFICIAL_LLM_URL", "https://llm.testing.example.test/v1")
        monkeypatch.setenv("AERO_DOWNLOAD_BASE", "https://cdn.example.test/download/testing")
    spec = importlib.util.spec_from_file_location(
        "release_prepare", ROOT / "scripts/release/prepare.py"
    )
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    output = tmp_path / "release"
    envelope = prepare.build(output, "v" + version, mirror=False)
    wheel = next((output / "pypi").glob("*.whl"))
    assert wheel.name.endswith("-py3-none-any.whl")
    with zipfile.ZipFile(wheel) as bundle:
        keys = json.loads(bundle.read("aero/release_keys.json"))
        assert base64.b64encode(key.public_key().public_bytes_raw()).decode() in keys.values()
        assert verify_manifest(envelope, keys)["version"] == version
        assert "aero/cli/update.py" in bundle.namelist()
        assert "aero/server/static/index.html" in bundle.namelist()
        assert seed.encode() not in bundle.read("aero/release_keys.json")
    with tarfile.open(next((output / "pypi").glob("*.tar.gz"))) as bundle:
        names = bundle.getnames()
        assert any(name.endswith("/src/aero/release_keys.json") for name in names)
        assert any(name.endswith("/scripts/release/prepare.py") for name in names)
    copied = output / "download" / envelope["manifest"]["wheel"]["path"]
    assert copied.read_bytes() == wheel.read_bytes()
    installer = (output / "download/install.sh").read_text()
    assert f"AERO_RELEASE_DEFAULT_AERO_ENVIRONMENT={environment}" in installer
    assert envelope["manifest"]["runtime"]["download_base"] in installer
    with zipfile.ZipFile(wheel) as bundle:
        assert (
            json.loads(bundle.read("aero/release_environment.json"))
            == envelope["manifest"]["runtime"]
        )
        installed = tmp_path / "installed"
        for name in (
            "aero/__init__.py",
            "aero/distribution.py",
            "aero/release_environment.py",
            "aero/release_environment.json",
        ):
            bundle.extract(name, installed)
    runtime_check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; sys.path.insert(0,sys.argv[1]); "
            "from aero.release_environment import configure_runtime,distribution_profile; "
            "from aero.distribution import default_download_base,install_environment; "
            "configure_runtime(); p=distribution_profile(); "
            "assert default_download_base()==p['download_base']; "
            "assert install_environment()==p['environment']; print(json.dumps(p))",
            str(installed),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(runtime_check.stdout) == envelope["manifest"]["runtime"]
    assert (output / "download/stable.json").is_file()
    second = tmp_path / "repeat"
    repeated = prepare.build(second, "v" + version, mirror=False)
    assert repeated == envelope
    for archive in (output / "download/releases" / version).glob("*.tar.gz"):
        assert (
            archive.read_bytes()
            == (second / "download/releases" / version / archive.name).read_bytes()
        )
