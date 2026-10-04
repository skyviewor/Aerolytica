"""Compatibility helpers re-exported by the legacy builtin_tools module."""

from __future__ import annotations

from pathlib import Path

from aero.toolbox.config_access import find_config, find_config_path
from aero.toolbox.download_progress import (
    download_progress_reporter,
    format_duration,
    format_size,
)
from aero.toolbox.paths import find_project_dir, short_path
from aero.toolbox.runtime_manager import get_runtime_tool_manager
from aero.toolbox.tools.local_data import scan_local_files as scan_local_files


def _find_config():
    """Compatibility wrapper for shared configuration lookup."""
    return find_config()


def _find_config_path() -> Path:
    """Compatibility wrapper for shared configuration path lookup."""
    return find_config_path()


def _find_project_dir() -> Path:
    """Compatibility wrapper for shared project path lookup."""
    return find_project_dir()


def _short_path(path: str | Path) -> str:
    """Compatibility wrapper for shared path shortening."""
    return short_path(path)


def _runtime_tools_ready(tools: list[str], env: dict[str, str]):
    """Compatibility wrapper; use RuntimeToolManager.tools_ready in new code."""
    return get_runtime_tool_manager().tools_ready(tools, env)


def _fmt_size(size: int) -> str:
    return format_size(size)


def _download_progress_reporter():
    return download_progress_reporter()


def _fmt_duration(seconds: float) -> str:
    return format_duration(seconds)
