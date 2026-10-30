"""Build one signed release; no publishing credentials are needed here."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
TARGETS = {
    "macos-aarch64": "aarch64-apple-darwin",
    "macos-x86_64": "x86_64-apple-darwin",
    "linux-aarch64": "aarch64-unknown-linux-gnu",
    "linux-x86_64": "x86_64-unknown-linux-gnu",
}


def distribution_module():
    spec = importlib.util.spec_from_file_location(
        "release_distribution", ROOT / "src/aero/distribution.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def get_signing_key() -> Ed25519PrivateKey:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = os.environ.get("RELEASE_SIGNING_KEY", "")
    try:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw, validate=True))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "RELEASE_SIGNING_KEY must be a base64-encoded Ed25519 32-byte seed"
        ) from exc


def signing_keys(key: Ed25519PrivateKey) -> tuple[str, dict[str, str]]:
    public = key.public_key().public_bytes_raw()
    identifier = hashlib.sha256(public).hexdigest()[:16]
    keys = json.loads((ROOT / "src/aero/release_keys.json").read_text())
    keys.update(json.loads(os.environ.get("RELEASE_PUBLIC_KEYS", "{}")))
    keys[identifier] = base64.b64encode(public).decode()
    return identifier, keys


def release_profile(module) -> dict:
    name = os.environ.get("AERO_ENVIRONMENT", "production")
    strict = name != "production" or os.environ.get("AERO_RELEASE_REQUIRE_CONFIG") == "1"
    profile = {"environment": name}
    for field, variable in (
        ("api_url", "AERO_OFFICIAL_API_URL"),
        ("llm_url", "AERO_OFFICIAL_LLM_URL"),
        ("download_base", "AERO_DOWNLOAD_BASE"),
    ):
        value = os.environ.get(variable, "")
        if strict and not value:
            raise RuntimeError(f"Missing release configuration: {variable}")
        profile[field] = value or module.DEFAULT_RUNTIME_PROFILE[field]
    return module.validate_runtime_profile(profile)


def configured_installer(source: str, profile: dict) -> str:
    for variable, value in (
        ("AERO_ENVIRONMENT", profile["environment"]),
        ("AERO_DOWNLOAD_BASE", profile["download_base"]),
    ):
        marker = next(line for line in source.splitlines() if line.startswith(variable + "="))
        # URLs are untrusted CI configuration, not shell source.
        fallback = f"AERO_RELEASE_DEFAULT_{variable}={shlex.quote(value)}"
        replacement = fallback + f'\n{variable}="${{{variable}:-$AERO_RELEASE_DEFAULT_{variable}}}"'
        source = source.replace(marker, replacement, 1)
    return source


def build(output: Path, tag: str, *, mirror: bool = True) -> dict:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = metadata["version"]
    if metadata["name"] != "aerolytica" or tag != f"v{version}":
        raise RuntimeError("Release tag must exactly match v<pyproject.toml version>")
    key = get_signing_key()
    module = distribution_module()
    profile = release_profile(module)
    revision = os.environ.get("AERO_RELEASE_REVISION", "")
    if revision and (
        profile["environment"] != "testing" or not re.fullmatch(r"[0-9a-f]{40}", revision)
    ):
        raise RuntimeError("Only testing releases may use a full commit SHA revision")
    identifier, keys = signing_keys(key)
    toolchain = json.loads((ROOT / "scripts/release/toolchain.json").read_text())
    installer = (ROOT / "install.sh").read_text()
    for name, value in (
        ("UV_VERSION", toolchain["uv"]),
        ("PYTHON_VERSION", toolchain["python"]),
        ("PYTHON_BUILD", toolchain["python_build"]),
    ):
        if f'{name}="{value}"' not in installer:
            raise RuntimeError("Installer/toolchain version mismatch")
    pypi = output / "pypi"
    downloads = output / "download"
    release = downloads / "releases" / version
    if revision:
        release = release / "revisions" / revision
    pypi.mkdir(parents=True, exist_ok=True)
    release.mkdir(parents=True, exist_ok=True)
    if any(pypi.iterdir()):
        raise RuntimeError("Use a clean output directory; refusing stale artifacts")
    with tempfile.TemporaryDirectory(prefix="aero-build-") as temporary:
        staging = Path(temporary) / "project"
        staging.mkdir()
        for name in ("src", "webui", "scripts", "tests", "docs"):
            shutil.copytree(
                ROOT / name,
                staging / name,
                ignore=shutil.ignore_patterns("__pycache__", "node_modules"),
            )
        for name in ("pyproject.toml", "README.md", "LICENSE", "uv.lock", "install.sh"):
            source = ROOT / name
            if source.exists():
                shutil.copy2(source, staging / name)
        (staging / "src/aero/release_keys.json").write_text(json.dumps(keys, sort_keys=True))
        (staging / "src/aero/release_environment.json").write_text(
            json.dumps(profile, sort_keys=True)
        )
        subprocess.run(["uv", "build", "--out-dir", str(pypi.resolve()), str(staging)], check=True)
    wheel = next(pypi.glob("*.whl"))
    shutil.copy2(wheel, release / wheel.name)
    requirements = release / "requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-header",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements.resolve()),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    shutil.copy2(ROOT / "scripts/release/bootstrap.py", release / "bootstrap.py")
    configured = configured_installer(installer, profile)
    (release / "install.sh").write_text(configured)
    (downloads / "install.sh").write_text(configured)
    manifest = {
        "schema": 1,
        "distribution": "aerolytica",
        "version": version,
        "python": toolchain["python"],
        "uv": toolchain["uv"],
        "notes": os.environ.get("RELEASE_NOTES", "")[:8000],
        "runtime": profile,
        "platforms": {},
    }
    if revision:
        manifest["revision"] = revision
    for field, path in (
        ("wheel", release / wheel.name),
        ("requirements", requirements),
        ("bootstrap", release / "bootstrap.py"),
    ):
        manifest[field] = {
            "path": path.relative_to(downloads).as_posix(),
            "sha256": file_digest(path),
        }
    for name, target in TARGETS.items():
        python_path = f"bootstrap/python/{toolchain['python_build']}/{name}.tar.gz"
        if mirror:
            python_file = downloads / python_path
            upstream = (
                f"https://github.com/astral-sh/python-build-standalone/releases/download/"
                f"{toolchain['python_build']}/cpython-{toolchain['python']}+"
                f"{toolchain['python_build']}-{target}-install_only.tar.gz"
            )
            mirror_file(module, upstream, python_file)
            uv_file = downloads / f"bootstrap/uv/{toolchain['uv']}/{target}.tar.gz"
            mirror_file(
                module,
                f"https://github.com/astral-sh/uv/releases/download/"
                f"{toolchain['uv']}/uv-{target}.tar.gz",
                uv_file,
            )
            python_hash = file_digest(python_file)
        else:
            python_hash = "0" * 64  # Only for isolated build-contract tests, never publish.
        manifest["platforms"][name] = {"path": python_path, "sha256": python_hash}
        archive_name = "aero-" + name.replace("macos-aarch64", "macos-arm64") + ".tar.gz"
        archive = release / archive_name
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
        with (
            archive.open("wb") as raw,
            gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=epoch) as zipped,
        ):
            with tarfile.open(fileobj=zipped, mode="w") as bundle:
                info = tarfile.TarInfo(wheel.name)
                info.size = wheel.stat().st_size
                info.mode = 0o644
                info.mtime = epoch
                with wheel.open("rb") as contents:
                    bundle.addfile(info, contents)
        checksum(release / archive_name)
    envelope = {
        "manifest": manifest,
        "key_id": identifier,
        "signature": base64.b64encode(key.sign(module.canonical_manifest(manifest))).decode(),
    }
    (release / "release.json").write_text(json.dumps(envelope, sort_keys=True))
    (downloads / "stable.json").write_text(json.dumps(envelope, sort_keys=True))
    checksum(release / wheel.name)
    return envelope


def checksum(path: Path) -> None:
    path.with_name(path.name + ".sha256").write_text(f"{file_digest(path)}  {path.name}\n")


def mirror_file(module, url: str, destination: Path) -> None:
    # Upstream TLS authenticates bootstrap tools; publish and installer additionally
    # pin hashes. The signed manifest pins the Python archive used during updates.
    module.download(url, destination)
    checksum(destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/release")
    args = parser.parse_args()
    try:
        build(args.output.resolve(), args.tag)
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        raise SystemExit(f"Release preparation failed: {exc}") from exc


if __name__ == "__main__":
    main()
