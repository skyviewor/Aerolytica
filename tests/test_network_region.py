"""Tests for package mirror selection."""

import json

from aero.core.network_region import apply_package_mirrors, detect_network_region


class _Response:
    def __init__(self, country: str):
        self.payload = json.dumps({"country": country}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self.payload


def test_detect_network_region_uses_country_response():
    def opener(_url, timeout):
        return _Response("CN")

    assert detect_network_region({}, opener) == "mainland_china"


def test_detect_network_region_honors_override_without_network():
    def fail(*_args, **_kwargs):
        raise AssertionError("network should not be queried")

    assert detect_network_region({"AERO_NETWORK_REGION": "global"}, fail) == "global"


def test_detect_network_region_falls_back_to_timezone():
    def fail(*_args, **_kwargs):
        raise OSError("offline")

    assert detect_network_region({"TZ": "Asia/Shanghai"}, fail) == "mainland_china"
    assert detect_network_region({"TZ": "America/New_York"}, fail) == "global"


def test_package_mirrors_apply_in_mainland_china_and_preserve_overrides():
    env = apply_package_mirrors(
        {
            "AERO_NETWORK_REGION": "cn",
            "PIP_INDEX_URL": "https://example.test/simple",
        }
    )

    assert env["PIP_INDEX_URL"] == "https://example.test/simple"
    assert env["CONDA_CHANNEL_ALIAS"].endswith("/anaconda/cloud")
    assert env["MAMBA_CHANNEL_ALIAS"] == env["CONDA_CHANNEL_ALIAS"]
