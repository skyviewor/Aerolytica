"""Paths for Aero's private scientific runtime."""

from __future__ import annotations

import os
from pathlib import Path

from aero.release_environment import user_home

RUNTIME_ROOT_CONFIG = "runtime-root"


def aero_home() -> Path:
    """Return Aero's user data directory."""
    return user_home()


def runtime_root() -> Path:
    """Return the root owned exclusively by Aero's Micromamba runtime."""
    configured = os.environ.get("AERO_RUNTIME_ROOT")
    if configured:
        return Path(configured).expanduser()
    config_file = aero_home() / RUNTIME_ROOT_CONFIG
    try:
        persisted = config_file.read_text(encoding="utf-8").strip()
    except OSError:
        persisted = ""
    return Path(persisted).expanduser() if persisted else aero_home() / "runtime"


def save_runtime_root(path: Path) -> None:
    """Persist the selected runtime root for future Aero processes."""
    config_file = aero_home() / RUNTIME_ROOT_CONFIG
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(f"{path}\n", encoding="utf-8")


def micromamba_path() -> Path:
    """Return the managed Micromamba executable path."""
    configured = os.environ.get("AERO_MICROMAMBA")
    name = "micromamba.exe" if os.name == "nt" else "micromamba"
    return Path(configured).expanduser() if configured else runtime_root() / "bin" / name


def runtime_env_path() -> Path:
    """Return the fixed prefix for the aero-agent environment."""
    configured = os.environ.get("AERO_RUNTIME_ENV")
    return Path(configured).expanduser() if configured else runtime_root() / "envs" / "aero-agent"


def runtime_bin_path() -> Path:
    return runtime_env_path() / ("Scripts" if os.name == "nt" else "bin")


def runtime_python_path() -> Path:
    return runtime_bin_path() / ("python.exe" if os.name == "nt" else "python")
