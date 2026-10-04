"""Non-secret release defaults and isolated user state, shared by every frontend."""

from __future__ import annotations

import json
import os
from pathlib import Path

from aero.distribution import DEFAULT_RUNTIME_PROFILE, DistributionError, validate_runtime_profile

PROFILE_PATH = Path(__file__).with_name("release_environment.json")


def distribution_profile() -> dict:
    """Installed defaults are pinned to the wheel; ordinary source installs remain production."""
    try:
        value = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(DEFAULT_RUNTIME_PROFILE)
    except (OSError, ValueError) as exc:
        raise DistributionError("无法读取发行环境配置；请重新安装官方客户端。") from exc
    return validate_runtime_profile(value)


def environment_name() -> str:
    profile = distribution_profile()
    name = os.environ.get("AERO_ENVIRONMENT", profile["environment"])
    if name not in {"production", "testing"}:
        raise DistributionError("AERO_ENVIRONMENT 只能为 production 或 testing。")
    if PROFILE_PATH.exists() and name != profile["environment"]:
        raise DistributionError(
            "当前安装包所属环境与 AERO_ENVIRONMENT 不一致，请使用对应环境的入口。"
        )
    return name


def official_url(kind: str) -> str:
    name = environment_name()
    variable = {"api_url": "AERO_OFFICIAL_API_URL", "llm_url": "AERO_OFFICIAL_LLM_URL"}[kind]
    override = os.environ.get(variable)
    if override:
        return override.rstrip("/")
    profile = distribution_profile()
    if name != profile["environment"]:
        raise DistributionError(f"源码测试环境必须明确设置 {variable}，不能回退连接生产。")
    return profile[kind]


def user_home() -> Path:
    configured = os.environ.get("AERO_HOME")
    if configured:
        return Path(configured).expanduser()
    suffix = ".aero-testing" if environment_name() == "testing" else ".aero"
    return Path.home() / suffix


def configure_runtime() -> None:
    """Validate startup defaults without freezing them into the process environment."""
    environment_name()
    official_url("api_url")
    official_url("llm_url")
    # Do not export derived URLs: after upgrading, children must read the new wheel defaults.
