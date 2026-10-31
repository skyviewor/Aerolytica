"""Execute the real POSIX selector with fake HTTPS objects, never a real install."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://testing.example.test/download"
REVISION = "a" * 40


@pytest.fixture
def installer(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "fetches.jsonl"
    fixtures = tmp_path / "objects.json"
    curl = tools / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
url = next(arg for arg in args if arg.startswith("https://"))
objects = json.loads(Path(os.environ["AERO_TEST_CURL_FIXTURES"]).read_text())
with open(os.environ["AERO_TEST_CURL_LOG"], "a") as handle:
    handle.write(json.dumps(url) + "\\n")
item = objects.get(url, {"status": 404, "body": "not found"})
Path(args[args.index("-o") + 1]).write_text(item["body"])
if "-w" in args:
    print(item.get("status", 200), end="")
if "-fLsS" in args and item.get("status", 200) != 200:
    sys.exit(22)
"""
    )
    curl.chmod(0o700)
    prefix = tmp_path / "client"
    # The fixture overrides uname so these deterministic private tool paths
    # work on every CI host, without downloading/executing an archive.
    uname = tools / "uname"
    uname.write_text('#!/bin/sh\ncase "$1" in -s) echo Darwin ;; -m) echo arm64 ;; esac\n')
    uname.chmod(0o700)
    platform = "macos-aarch64"
    python = prefix / f"python/3.12.12-{platform}/python/bin/python3.12"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    uv = prefix / f"bootstrap/uv-0.12.23-{platform}/uv"
    uv.parent.mkdir(parents=True)
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o700)
    # Run all of the real installer through bootstrap, but not shell-profile
    # modification. No HOME override or access to the user's installation.
    source = (ROOT / "install.sh").read_text().split("\nPROFILE_LINE=", 1)[0]
    script = tmp_path / "install.sh"
    script.write_text(source + "\n")
    coordinator = """import argparse,json,os
p=argparse.ArgumentParser()
for key in ("base","uv","manifest","wheel"): p.add_argument("--"+key)
a=p.parse_args()
m=json.load(open(a.manifest))["manifest"]
print(json.dumps({"version":m["version"],"revision":m.get("revision"),
"base":a.base,"downgrade":os.environ.get("AERO_ALLOW_DOWNGRADE")}))
"""
    objects = {}
    helper_hash = hashlib.sha256(coordinator.encode()).hexdigest()
    wheel_hash = hashlib.sha256(b"mock wheel").hexdigest()

    def release(version, revision=None, environment="testing"):
        location = f"releases/{version}/" + (f"revisions/{revision}/" if revision else "")
        manifest = {
            "schema": 1,
            "distribution": "aerolytica",
            "version": version,
            "runtime": {"environment": environment},
            "wheel": {"path": location + "client.whl", "sha256": wheel_hash},
            "bootstrap": {"path": location + "bootstrap.py", "sha256": helper_hash},
        }
        if revision:
            manifest["revision"] = revision
        objects[BASE + "/" + location + "client.whl"] = {"body": "mock wheel"}
        objects[BASE + "/" + location + "bootstrap.py"] = {"body": coordinator}
        return {"body": json.dumps({"manifest": manifest})}

    objects[BASE + "/stable.json"] = release("0.1.3")
    objects[BASE + "/releases/0.1.1/release.json"] = release("0.1.1")
    objects[BASE + "/releases/0.1.2/latest.json"] = release("0.1.2", REVISION)
    objects[BASE + f"/releases/0.1.2/revisions/{REVISION}/release.json"] = release(
        "0.1.2", REVISION
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "AERO_VERSION",
            "AERO_INSTALL_REVISION",
            "AERO_ALLOW_DOWNGRADE",
            "AERO_REINSTALL",
            "AERO_RELEASE_MANIFEST",
            "PYTHONHOME",
            "PYTHONPATH",
        }
    }
    env.update(
        AERO_ENVIRONMENT="testing",
        AERO_DOWNLOAD_BASE=BASE,
        AERO_HOME=str(tmp_path / "state"),
        AERO_INSTALL_ROOT=str(prefix),
        AERO_BIN_DIR=str(tmp_path / "bin"),
        AERO_TEST_CURL_FIXTURES=str(fixtures),
        AERO_TEST_CURL_LOG=str(log),
        PATH=str(tools) + os.pathsep + env["PATH"],
    )

    def run(*args, **overrides):
        fixtures.write_text(json.dumps(objects))
        log.unlink(missing_ok=True)
        return subprocess.run(
            ["sh", str(script), *args],
            env={**env, **overrides},
            capture_output=True,
            text=True,
            timeout=15,
        )

    return run, objects, log, release


@pytest.mark.parametrize(
    "args,version,revision",
    [
        ([], "0.1.3", None),
        (["--version", "0.1.3"], "0.1.3", None),
        (["--version", "0.1.1"], "0.1.1", None),
        (["--version", "v0.1.2"], "0.1.2", REVISION),
        (["--version", "0.1.2", "--revision", REVISION], "0.1.2", REVISION),
    ],
)
def test_exact_version_selection_and_current_coordinator(installer, args, version, revision):
    run, _, log, _ = installer
    result = run(*args)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.splitlines()[-1])
    assert (data["version"], data["revision"], data["base"]) == (version, revision, BASE)
    urls = [json.loads(line) for line in log.read_text().splitlines()]
    assert BASE + "/releases/0.1.3/bootstrap.py" in urls
    if version != "0.1.3":
        assert not any(url.endswith("bootstrap.py") and "/0.1.3/" not in url for url in urls)


def test_environment_selector_and_downgrade_consent(installer):
    run, _, _, _ = installer
    result = run("--version", "0.1.1", "--allow-downgrade")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["downgrade"] == "1"
    result = run(AERO_VERSION="0.1.1")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["version"] == "0.1.1"


@pytest.mark.parametrize(
    "args",
    [
        ["--version", "../bad"],
        ["--version", "https://evil.test"],
        ["--version", "1"],
        ["--version"],
        ["--revision", REVISION],
        ["--allow-downgrade"],
        ["--version", "0.1.2", "--revision", "short"],
        ["--unknown"],
    ],
)
def test_invalid_selector_stops_before_any_download(installer, args):
    run, _, log, _ = installer
    assert run(*args).returncode != 0
    assert not log.exists()


def test_missing_version_never_silently_installs_latest(installer):
    run, _, _, _ = installer
    result = run("--version", "8.8.8")
    assert result.returncode != 0 and "指定版本不存在" in result.stderr


def test_bad_pointer_or_server_error_does_not_fall_back(installer):
    run, objects, log, release = installer
    key = BASE + "/releases/0.1.1/latest.json"
    objects[key] = release("0.1.3")
    result = run("--version", "0.1.1")
    assert result.returncode != 0 and "与指定版本不符" in result.stderr
    objects[key] = {"status": 503, "body": "unavailable"}
    result = run("--version", "0.1.1")
    assert result.returncode != 0 and "HTTP 503" in result.stderr
    assert "releases/0.1.1/release.json" not in log.read_text()


def test_cross_environment_and_conflicting_selectors_are_rejected(installer):
    run, objects, log, release = installer
    objects[BASE + "/releases/0.1.1/latest.json"] = release("0.1.1", environment="production")
    result = run("--version", "0.1.1")
    assert result.returncode != 0 and "跨环境" in result.stderr
    result = run("--version", "0.1.1", AERO_RELEASE_MANIFEST="releases/0.1.1/release.json")
    assert result.returncode != 0 and not log.exists()


def test_first_release_smoke_can_install_before_global_stable_exists(installer):
    run, objects, _, release = installer
    del objects[BASE + "/stable.json"]
    path = "releases/0.1.3/release.json"
    objects[BASE + "/" + path] = release("0.1.3")
    result = run(AERO_RELEASE_MANIFEST=path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["version"] == "0.1.3"
    result = run()
    assert result.returncode != 0 and "尚未发布默认版本" in result.stderr


def test_revision_mismatch_and_production_revision_are_rejected(installer):
    run, objects, log, release = installer
    key = BASE + f"/releases/0.1.2/revisions/{REVISION}/release.json"
    objects[key] = release("0.1.2", "b" * 40)
    result = run("--version", "0.1.2", "--revision", REVISION)
    assert result.returncode != 0 and "指定测试修订不符" in result.stderr
    result = run("--version", "0.1.2", "--revision", REVISION, AERO_ENVIRONMENT="production")
    assert result.returncode != 0 and "只适用于 testing" in result.stderr and not log.exists()
