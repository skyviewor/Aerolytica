"""Same-version testing revisions never overwrite production or active environments."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from aero import distribution as dist
from aero.cli import update


@pytest.fixture
def revision(monkeypatch, tmp_path):
    root = tmp_path / "client"
    monkeypatch.setenv("AERO_INSTALL_ROOT", str(root))
    monkeypatch.setenv("AERO_ENVIRONMENT", "testing")
    monkeypatch.setenv("AERO_DOWNLOAD_BASE", "https://testing.example.test/download")
    manifest = {
        "schema": 1,
        "distribution": "aerolytica",
        "version": "0.1.2",
        "python": "3.12.12",
        "revision": "a" * 40,
        "runtime": {
            **dist.DEFAULT_RUNTIME_PROFILE,
            "environment": "testing",
            "download_base": "https://testing.example.test/download",
        },
        "wheel": {
            "path": "releases/0.1.2/revisions/" + "a" * 40 + "/client.whl",
            "sha256": "a" * 64,
        },
        "requirements": {
            "path": "releases/0.1.2/revisions/" + "a" * 40 + "/requirements.txt",
            "sha256": "b" * 64,
        },
        "platforms": {"macos-aarch64": {"path": "bootstrap/python.tar.gz", "sha256": "c" * 64}},
    }
    old = root / "versions/0.1.2"
    dist.write_state(
        old / "aero-install.json",
        {
            "managed": True,
            "version": "0.1.2",
            "manifest_hash": "old",
            "runtime": manifest["runtime"],
        },
    )
    dist.activate(old)
    monkeypatch.setattr(update, "get_version", lambda: "0.1.2")
    monkeypatch.setattr(update, "fetch_release", lambda _: {"manifest": manifest})
    monkeypatch.setattr(
        update, "verify_manifest", lambda value: dist.validate_manifest(value["manifest"])
    )
    monkeypatch.setattr(
        dist, "verify_manifest", lambda value: dist.validate_manifest(value["manifest"])
    )
    return manifest, old


def test_reinstall_requires_explicit_choice_and_is_idempotent(revision):
    manifest, old = revision
    assert update.check_update(force=True) is None
    assert update.check_update(force=True, reinstall=True)["manifest"] == manifest
    fingerprint = hashlib.sha256(dist.canonical_manifest(manifest)).hexdigest()
    marker = json.loads((old / "aero-install.json").read_text())
    dist.write_state(old / "aero-install.json", {**marker, "manifest_hash": fingerprint})
    assert update.check_update(force=True, reinstall=True) is None


def test_reinstall_cannot_cross_versions_or_release_environments(revision):
    manifest, _ = revision
    manifest["version"] = "0.1.3"
    with pytest.raises(dist.DistributionError, match="跨版本"):
        update.check_update(force=True, reinstall=True)
    manifest["runtime"]["environment"] = "production"
    with pytest.raises(dist.DistributionError):
        update.check_update(force=True, reinstall=True)


@pytest.mark.parametrize("revision_value", ["../bad", "", "A" * 40, "f" * 39, None])
def test_revision_identifier_is_validated(revision, revision_value):
    manifest, _ = revision
    manifest["revision"] = revision_value
    with pytest.raises(dist.DistributionError):
        dist.validate_manifest(manifest)


def test_revision_stages_beside_old_version_without_changing_current(revision, monkeypatch):
    manifest, old = revision
    original = (old / "aero-install.json").read_bytes()
    monkeypatch.setattr(dist, "interpreter_version", lambda: "3.12.12")
    monkeypatch.setattr(dist, "download", lambda url, path, **kwargs: path.write_bytes(b"fixture"))
    monkeypatch.setattr(dist, "_run", Mock())
    envelope = {"manifest": manifest}
    new = dist.stage_release(
        envelope, Path("/uv"), "https://testing.example.test/download", bootstrap=True
    )
    assert new.name == "0.1.2-" + "a" * 40
    assert dist.current_environment() == old
    assert (old / "aero-install.json").read_bytes() == original
    second = copy.deepcopy(envelope)
    second["manifest"]["revision"] = "b" * 40
    other = dist.stage_release(
        second, Path("/uv"), "https://testing.example.test/download", bootstrap=True
    )
    assert new != other and new.exists() and other.exists()
