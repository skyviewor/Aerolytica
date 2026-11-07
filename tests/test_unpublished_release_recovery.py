"""The approved one-off recovery cannot delete stable or a published version."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def recovery(monkeypatch):
    sdk = ModuleType("oss2")
    monkeypatch.setitem(sys.modules, "oss2", sdk)
    spec = importlib.util.spec_from_file_location(
        "approved_recovery", ROOT / "scripts/release/cleanup_unpublished_013.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stable = {"manifest": {"version": "0.1.2", "runtime": {"environment": "production"}}}
    staged = {"manifest": {"version": "0.1.3", "wheel": {"sha256": module.OLD_WHEEL}}}
    objects = {
        "download/stable.json": json.dumps(stable).encode(),
        module.TARGET + "release.json": json.dumps(staged).encode(),
        module.TARGET + "client.whl": b"cancelled upload",
        "download/releases/0.1.2/client.whl": b"published version",
        "website/index.html": b"unrelated website",
    }
    bucket = Mock()
    bucket.get_object.side_effect = lambda key: io.BytesIO(objects[key])
    bucket.object_exists.side_effect = lambda key: key in objects
    bucket.delete_object.side_effect = lambda key: objects.pop(key)
    sdk.ObjectIteratorV2 = lambda bucket, prefix: [
        SimpleNamespace(key=key) for key in objects if key.startswith(prefix)
    ]
    return module, bucket, objects


def test_recovery_only_removes_approved_unpublished_files(recovery):
    module, bucket, objects = recovery
    assert module.cleanup(bucket) == 2
    assert set(objects) == {
        "download/stable.json", "download/releases/0.1.2/client.whl", "website/index.html"
    }
    assert bucket.delete_object.call_args_list[-1].args == (module.TARGET + "release.json",)
    assert module.cleanup(bucket) == 0


@pytest.mark.parametrize("change", ["stable", "promoted", "hash", "nested"])
def test_recovery_refuses_unapproved_state_before_any_deletion(recovery, change):
    module, bucket, objects = recovery
    if change == "stable":
        objects["download/stable.json"] = json.dumps({"manifest": {
            "version": "0.1.3", "runtime": {"environment": "production"}
        }}).encode()
    elif change == "promoted":
        objects[module.TARGET + "latest.json"] = b"already published"
    elif change == "hash":
        objects[module.TARGET + "release.json"] = json.dumps({"manifest": {
            "version": "0.1.3", "wheel": {"sha256": "a" * 64}
        }}).encode()
    else:
        objects[module.TARGET + "builds/new/client.whl"] = b"new candidate"
    with pytest.raises(RuntimeError):
        module.cleanup(bucket)
    bucket.delete_object.assert_not_called()
