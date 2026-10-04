from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]


def load_helper(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/release/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_key_pair_matches_release_format(monkeypatch):
    helper = load_helper("generate_keys")
    seed, public, key_id = helper.generate_key_pair()
    assert len(base64.b64decode(seed, validate=True)) == 32
    assert len(base64.b64decode(public, validate=True)) == 32
    prepare = load_helper("prepare")
    monkeypatch.setenv("RELEASE_SIGNING_KEY", seed)
    monkeypatch.setenv("RELEASE_PUBLIC_KEYS", "{}")
    signing_key = prepare.get_signing_key()
    identifier, keys = prepare.signing_keys(signing_key)
    assert identifier == key_id
    assert keys[key_id] == public
    signing_key.public_key().verify(signing_key.sign(b"release key smoke"), b"release key smoke")


def test_generate_keys_refuses_non_terminal_before_generating(monkeypatch, capsys):
    helper = load_helper("generate_keys")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    def unexpected_generation():
        raise AssertionError("Do not generate secrets for redirected output")

    monkeypatch.setattr(helper, "generate_key_pair", unexpected_generation)
    assert helper.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "拒绝" in captured.err


def test_generate_keys_labels_github_fields_and_environment(monkeypatch, capsys, tmp_path):
    helper = load_helper("generate_keys")
    # Public, deterministic test data: never display a real release key in test output.
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    seed = base64.b64encode(key.private_bytes_raw()).decode()
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    key_id = "test-key-id"
    monkeypatch.setattr(helper, "generate_key_pair", lambda: (seed, public, key_id))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.chdir(tmp_path)
    assert helper.main(["--environment", "testing"]) == 0
    captured = capsys.readouterr()
    assert "testing" in captured.out
    assert f"RELEASE_SIGNING_KEY（私钥，必须保密）\n{seed}" in captured.out
    assert json.dumps({key_id: public}) in captured.out
    assert "RELEASE_PUBLIC_KEYS" in captured.out
    assert "1Password" in captured.out
    assert "录屏" in captured.err
    assert not list(tmp_path.iterdir())
