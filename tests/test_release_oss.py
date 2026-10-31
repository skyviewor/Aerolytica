"""Exercise scoped OSS publishing without cloud credentials or network calls."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from aero import distribution

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def publisher(monkeypatch, tmp_path):
    objects = {}
    bucket = Mock()
    bucket.object_exists.side_effect = lambda key: key in objects
    bucket.get_object.side_effect = lambda key: io.BytesIO(objects[key])

    def put(key, filename, **kwargs):
        objects[key] = Path(filename).read_bytes()

    bucket.put_object_from_file.side_effect = put
    bucket.put_object.side_effect = lambda key, value, **kwargs: objects.__setitem__(key, value)
    sdk = ModuleType("oss2")
    sdk.Auth = Mock()
    sdk.Bucket = Mock(return_value=bucket)
    monkeypatch.setitem(sys.modules, "oss2", sdk)
    prepare = ModuleType("prepare")
    prepare.distribution_module = lambda: distribution
    monkeypatch.setitem(sys.modules, "prepare", prepare)
    spec = importlib.util.spec_from_file_location(
        "release_oss_test", ROOT / "scripts/release/publish_oss.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, value in {
        "OSS_ACCESS_KEY_ID": "test-only-id",
        "OSS_ACCESS_KEY_SECRET": "test-only-secret",
        "OSS_BUCKET": "testing-bucket",
        "OSS_ENDPOINT": "https://oss.example.test",
        "OSS_PREFIX": "download",
        "AERO_ENVIRONMENT": "testing",
        "AERO_DOWNLOAD_BASE": "https://testing.example.test/download",
    }.items():
        monkeypatch.setenv(name, value)
    manifest = {
        "schema": 1,
        "distribution": "aerolytica",
        "version": "0.1.0",
        "python": "3.12.12",
        "runtime": {
            "environment": "testing",
            "api_url": "https://api.testing.example.test",
            "llm_url": "https://llm.testing.example.test/v1",
            "download_base": "https://testing.example.test/download",
        },
        "wheel": {"path": "releases/0.1.0/client.whl", "sha256": "a" * 64},
        "requirements": {"path": "releases/0.1.0/requirements.txt", "sha256": "b" * 64},
        "platforms": {"linux-x86_64": {"path": "bootstrap/python.tar.gz", "sha256": "c" * 64}},
    }
    (tmp_path / "stable.json").write_text(json.dumps({"manifest": manifest}))
    (tmp_path / "install.sh").write_text("test installer")
    release_dir = tmp_path / "releases/0.1.0"
    release_dir.mkdir(parents=True)
    (release_dir / "client.whl").write_bytes(b"test wheel")
    monkeypatch.setattr(sys, "argv", ["publish_oss.py", "stage", "--directory", str(tmp_path)])
    return module, sdk, bucket, objects


def test_testing_bucket_can_use_download_without_nested_prefix(publisher):
    module, sdk, bucket, objects = publisher
    module.main()
    assert sdk.Bucket.call_args.args[2] == "testing-bucket"
    assert set(objects) == {"download/releases/0.1.0/client.whl"}
    assert bucket.put_object_from_file.call_count == 1
    module.main()  # Identical artifacts are reused, not overwritten.
    assert bucket.put_object_from_file.call_count == 1


@pytest.mark.parametrize("prefix", ["", "/", "../download", "download/../other"])
def test_unsafe_prefix_is_rejected(publisher, monkeypatch, prefix):
    module, sdk, bucket, objects = publisher
    monkeypatch.setenv("OSS_PREFIX", prefix)
    with pytest.raises(SystemExit, match="non-root safe"):
        module.main()
    sdk.Bucket.assert_not_called()


def test_existing_version_cannot_be_overwritten(publisher):
    module, sdk, bucket, objects = publisher
    objects["download/releases/0.1.0/client.whl"] = b"existing different artifact"
    with pytest.raises(RuntimeError, match="Immutable OSS artifact differs"):
        module.main()
    bucket.put_object_from_file.assert_not_called()


def test_new_testing_revision_preserves_the_existing_flat_release(publisher, tmp_path):
    module, _, bucket, objects = publisher
    old_key = "download/releases/0.1.0/client.whl"
    objects[old_key] = b"previous signed wheel"
    revision = "a" * 40
    old = tmp_path / "releases/0.1.0/client.whl"
    new = old.parent / "revisions" / revision / old.name
    new.parent.mkdir(parents=True)
    old.rename(new)
    release = json.loads((tmp_path / "stable.json").read_text())
    release["manifest"]["revision"] = revision
    for name in ("wheel", "requirements"):
        release["manifest"][name]["path"] = release["manifest"][name]["path"].replace(
            "releases/0.1.0/", "releases/0.1.0/revisions/" + revision + "/"
        )
    (tmp_path / "stable.json").write_text(json.dumps(release))
    module.main()
    assert objects[old_key] == b"previous signed wheel"
    assert objects["download/" + release["manifest"]["wheel"]["path"]] == b"test wheel"
    assert bucket.put_object_from_file.call_count == 1


@pytest.mark.parametrize("field", ["AERO_ENVIRONMENT", "AERO_DOWNLOAD_BASE"])
def test_release_environment_and_download_mapping_still_checked(publisher, monkeypatch, field):
    module, sdk, bucket, objects = publisher
    monkeypatch.setenv(field, "production" if field == "AERO_ENVIRONMENT" else "https://wrong.test")
    with pytest.raises((distribution.DistributionError, SystemExit)):
        module.main()
    bucket.put_object_from_file.assert_not_called()


def test_promote_records_a_per_version_signed_pointer(publisher, monkeypatch, tmp_path):
    module, _, bucket, objects = publisher
    monkeypatch.setattr(sys, "argv", ["publish_oss.py", "promote", "--directory", str(tmp_path)])
    module.main()
    assert objects["download/releases/0.1.0/latest.json"] == objects["download/stable.json"]
    assert objects["download/releases/0.1.0/latest.json"] == (tmp_path / "stable.json").read_bytes()
    headers = bucket.put_object_from_file.call_args_list[0].kwargs["headers"]
    assert "no-cache" in headers["Cache-Control"] and "immutable" not in headers["Cache-Control"]


def test_older_version_pointer_does_not_downgrade_global_latest(publisher, monkeypatch, tmp_path):
    module, _, _, objects = publisher
    latest = json.loads((tmp_path / "stable.json").read_text())
    latest["manifest"]["version"] = "0.2.0"
    objects["download/stable.json"] = json.dumps(latest).encode()
    before = objects["download/stable.json"]
    monkeypatch.setattr(sys, "argv", ["publish_oss.py", "promote", "--directory", str(tmp_path)])
    module.main()
    assert objects["download/stable.json"] == before
    assert objects["download/releases/0.1.0/latest.json"] == (tmp_path / "stable.json").read_bytes()


def test_promote_preserves_legacy_stable_revision_for_later_install(
    publisher, monkeypatch, tmp_path
):
    module, _, bucket, objects = publisher
    previous = json.loads((tmp_path / "stable.json").read_text())
    previous["manifest"]["version"] = "0.0.9"
    previous["manifest"]["revision"] = "b" * 40
    previous_bytes = json.dumps(previous).encode()
    objects["download/stable.json"] = previous_bytes
    monkeypatch.setattr(sys, "argv", ["publish_oss.py", "promote", "--directory", str(tmp_path)])
    module.main()
    assert objects["download/releases/0.0.9/latest.json"] == previous_bytes
    assert objects["download/stable.json"] == (tmp_path / "stable.json").read_bytes()
    bucket.put_object.assert_called_once()
    # A later retry must not overwrite an already published version pointer.
    objects["download/stable.json"] = previous_bytes
    objects["download/releases/0.0.9/latest.json"] = b"existing accepted pointer"
    module.main()
    assert objects["download/releases/0.0.9/latest.json"] == b"existing accepted pointer"
    bucket.put_object.assert_called_once()
