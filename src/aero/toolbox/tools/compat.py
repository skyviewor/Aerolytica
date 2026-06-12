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


# TODO: scan_local_files — scan local data files and register them in download_store
# 1. Scan target directory for data files (.nc/.grb/.grib) not already in the store
# 2. Call inspect_nc on each new file to get variables, dimensions, shape
# 3. Group files by filename pattern (e.g. era5_t_z_pl500_2023*.nc)
# 4. Return candidate groups to Agent → confirm metadata with user
# 5. Re-call with confirm=True to write confirmed records (source='local', status='confirmed')
async def scan_local_files(directory: str, pattern: str = "*.nc", confirm: bool = False) -> dict:
    raise NotImplementedError("scan_local_files is not yet implemented")


def _fmt_size(size: int) -> str:
    return format_size(size)


def _download_progress_reporter():
    return download_progress_reporter()


def _fmt_duration(seconds: float) -> str:
    return format_duration(seconds)
