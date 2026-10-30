from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import shlex
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


def test_upload_helpers_do_not_require_signing_dependencies():
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "from prepare import distribution_module, file_digest; "
            "assert distribution_module().DEFAULT_RUNTIME_PROFILE; assert callable(file_digest)",
        ],
        cwd=ROOT / "scripts/release",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_release_order_secret_scope_and_immutable_actions():
    workflow = yaml.load(
        (ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert workflow["on"]["release"]["types"] == ["published"]
    assert workflow["on"]["push"]["branches"] == ["testing"]
    assert workflow["on"]["workflow_dispatch"]["inputs"]["environment"]["default"] == "testing"
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    jobs = workflow["jobs"]
    assert set(jobs) == {"build", "stage-oss", "install-smoke", "promote-oss"}
    assert workflow["permissions"] == {"contents": "read"}
    assert jobs["stage-oss"]["needs"] == "build"
    assert jobs["install-smoke"]["needs"] == ["build", "stage-oss"]
    assert jobs["promote-oss"]["needs"] == ["build", "install-smoke"]
    assert jobs["install-smoke"]["env"]["RELEASE_TAG"] == ("${{ needs.build.outputs.release_tag }}")
    for job_name in ("stage-oss", "promote-oss"):
        assert jobs[job_name]["steps"][0]["with"]["ref"] == (
            "${{ needs.build.outputs.source_sha }}"
        )
    for job in jobs.values():
        assert job["environment"] == (
            "${{ github.event_name == 'release' && 'production' "
            "|| inputs.environment || 'testing' }}"
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


@pytest.mark.parametrize(
    ("event", "environment", "ref", "tag", "succeeds"),
    [
        ("push", "testing", "refs/heads/testing", "", True),
        ("push", "production", "refs/heads/testing", "", False),
        ("push", "testing", "refs/heads/main", "", False),
        ("workflow_dispatch", "testing", "refs/tags/v0.1.0", "v0.1.0", True),
        ("workflow_dispatch", "testing", "refs/tags/v0.2.0", "v0.2.0", False),
        ("workflow_dispatch", "production", "refs/tags/v0.1.0", "v0.1.0", False),
        ("release", "production", "refs/tags/v0.1.0", "v0.1.0", False),
    ],
)
def test_source_gate_allows_testing_but_rejects_unmerged_production(
    tmp_path, event, environment, ref, tag, succeeds
):
    workflow = yaml.load(
        (ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader
    )
    step = next(
        step for step in workflow["jobs"]["build"]["steps"] if step.get("id") == "release_identity"
    )
    script_directory = tmp_path / "scripts/release"
    script_directory.mkdir(parents=True)
    shutil.copy(ROOT / "scripts/release/validate_artifacts.py", script_directory)
    (tmp_path / "pyproject.toml").write_text('[project]\nname="aerolytica"\nversion="0.1.0"\n')
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "uv").write_text(
        "#!/bin/sh\nshift 3\nexec " + shlex.quote(sys.executable) + ' "$@"\n'
    )
    (binaries / "git").write_text(
        '#!/bin/sh\ncase "$1" in\n'
        "merge-base) exit 1 ;;\n"
        'rev-parse) printf "%s\\n" aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;\n'
        'show) printf "%s\\n" 1728000000 ;;\n'
        "*) exit 2 ;;\nesac\n"
    )
    for binary in binaries.iterdir():
        binary.chmod(0o755)
    output = tmp_path / "outputs"
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(binaries)
            + os.pathsep
            + str(Path(sys.executable).parent)
            + os.pathsep
            + os.environ["PATH"],
            "GITHUB_EVENT_NAME": event,
            "GITHUB_REF": ref,
            "RELEASE_ENVIRONMENT": environment,
            "RELEASE_TAG": tag,
            "GITHUB_ENV": str(tmp_path / "environment"),
            "GITHUB_OUTPUT": str(output),
        },
    )
    assert (result.returncode == 0) is succeeds, result.stderr
    if succeeds:
        assert "release_tag=v0.1.0\n" in output.read_text()
        assert "source_sha=" + "a" * 40 in output.read_text()
    else:
        assert not output.exists()


def test_testpypi_is_paused_unless_explicitly_enabled():
    workflow = yaml.load(
        (ROOT / ".github/workflows/testpypi.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["jobs"]["rehearsal"]["if"] == ("${{ vars.ENABLE_TESTPYPI_PUBLISH == 'true' }}")


@pytest.mark.parametrize("workflow_name", ["ci.yml", "release.yml"])
def test_offline_build_test_dependencies_are_warmed_before_pytest(workflow_name):
    workflow = yaml.load(
        (ROOT / ".github/workflows" / workflow_name).read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["checks" if workflow_name == "ci.yml" else "build"]["steps"]
    runs = [step.get("run", "") for step in steps]
    assert runs.index('uv build --out-dir "$RUNNER_TEMP/aero-build-check"') < runs.index(
        "uv run --locked pytest"
    )


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required for a real release build")
@pytest.mark.parametrize(
    "environment,revision", [("production", ""), ("testing", ""), ("testing", "a" * 40)]
)
def test_real_signed_wheel_sdist_and_manifest_are_consistent(
    monkeypatch, tmp_path, environment, revision
):
    key = Ed25519PrivateKey.generate()
    seed = base64.b64encode(key.private_bytes_raw()).decode()
    monkeypatch.setenv("RELEASE_SIGNING_KEY", seed)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1728000000")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("AERO_ENVIRONMENT", environment)
    monkeypatch.setenv("AERO_RELEASE_REVISION", revision)
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
    if revision:
        assert envelope["manifest"]["revision"] == revision
        assert f"/revisions/{revision}/" in envelope["manifest"]["wheel"]["path"]
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
    relative = Path(envelope["manifest"]["wheel"]["path"]).parent
    for archive in (output / "download" / relative).glob("*.tar.gz"):
        assert archive.read_bytes() == (second / "download" / relative / archive.name).read_bytes()


def test_testing_smoke_uses_the_exact_signed_revision_and_retries_transport_errors():
    workflow = yaml.load(
        (ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader
    )
    smoke = workflow["jobs"]["install-smoke"]
    assert smoke["env"]["RELEASE_ARTIFACT_PREFIX"] == "${{ needs.build.outputs.artifact_prefix }}"
    script = smoke["steps"][0]["run"]
    assert "$RELEASE_ARTIFACT_PREFIX/release.json" in script
    assert "$RELEASE_ARTIFACT_PREFIX/install.sh" in script
    assert "--retry-all-errors" in script
