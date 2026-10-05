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


@pytest.mark.parametrize("field", ["AERO_ENVIRONMENT", "AERO_DOWNLOAD_BASE"])
def test_release_environment_and_download_mapping_still_checked(publisher, monkeypatch, field):
    module, sdk, bucket, objects = publisher
    monkeypatch.setenv(field, "production" if field == "AERO_ENVIRONMENT" else "https://wrong.test")
    with pytest.raises((distribution.DistributionError, SystemExit)):
        module.main()
    bucket.put_object_from_file.assert_not_called()
