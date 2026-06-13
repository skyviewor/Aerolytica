"""Network-region detection and package mirror configuration."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Callable

PYPI_CHINA_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
CONDA_CHINA_CHANNEL_ALIAS = "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud"
_COUNTRY_ENDPOINT = "https://api.country.is/"


def _timezone_name(env: dict[str, str]) -> str:
    if env.get("TZ"):
        return env["TZ"]
    localtime = Path("/etc/localtime")
    try:
        target = localtime.resolve()
    except OSError:
        target = localtime
    marker = "/zoneinfo/"
    if marker in str(target):
        return str(target).split(marker, 1)[1]
    return time.tzname[0]


def _normalize_override(value: str | None) -> str | None:
    normalized = (value or "").strip().lower().replace("-", "_")
    if normalized in {"cn", "china", "mainland", "mainland_china"}:
        return "mainland_china"
    if normalized in {"global", "international", "overseas", "non_cn"}:
        return "global"
    return None


def _detect_network_region(
    env: dict[str, str],
    opener: Callable[..., object],
) -> str:
    override = _normalize_override(env.get("AERO_NETWORK_REGION"))
    if override:
        return override
    try:
        with opener(_COUNTRY_ENDPOINT, timeout=2) as response:
            payload = json.loads(response.read().decode())
        return "mainland_china" if payload.get("country") == "CN" else "global"
    except Exception:
        return (
            "mainland_china"
            if _timezone_name(env) in {"Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin"}
            else "global"
        )


@lru_cache(maxsize=1)
def _cached_network_region() -> str:
    return _detect_network_region(dict(os.environ), urllib.request.urlopen)


def detect_network_region(
    env: dict[str, str] | None = None,
    opener: Callable[..., object] | None = None,
) -> str:
    """Return ``mainland_china`` or ``global`` without failing the caller."""
    if env is None and opener is None:
        if os.environ.get("AERO_NETWORK_REGION"):
            return _detect_network_region(dict(os.environ), urllib.request.urlopen)
        return _cached_network_region()
    source_env = dict(os.environ if env is None else env)
    return _detect_network_region(source_env, opener or urllib.request.urlopen)


def apply_package_mirrors(env: dict[str, str]) -> dict[str, str]:
    """Apply mainland-China package mirrors while preserving explicit settings."""
    configured = env.copy()
    region = (
        detect_network_region(configured)
        if configured.get("AERO_NETWORK_REGION")
        else detect_network_region()
    )
    if region != "mainland_china":
        return configured
    configured.setdefault("PIP_INDEX_URL", PYPI_CHINA_MIRROR)
    configured.setdefault("CONDA_CHANNEL_ALIAS", CONDA_CHINA_CHANNEL_ALIAS)
    configured.setdefault("MAMBA_CHANNEL_ALIAS", CONDA_CHINA_CHANNEL_ALIAS)
    return configured
