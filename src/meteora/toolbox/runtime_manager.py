"""Managed command-line tool discovery and execution dependencies."""

# ruff: noqa: E501

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from meteora.agent.runtime import _conda_roots

RUNTIME_TOOL_PACKAGES = {
    "cdo": ("cdo", ["cdo"]),
    "grib_to_netcdf": (
        "eccodes",
        ["grib_to_netcdf", "grib_copy", "grib_filter", "grib_ls", "grib_dump"],
    ),
    "grib_copy": (
        "eccodes",
        ["grib_to_netcdf", "grib_copy", "grib_filter", "grib_ls", "grib_dump"],
    ),
    "grib_filter": (
        "eccodes",
        ["grib_to_netcdf", "grib_copy", "grib_filter", "grib_ls", "grib_dump"],
    ),
    "grib_ls": ("eccodes", ["grib_to_netcdf", "grib_copy", "grib_filter", "grib_ls", "grib_dump"]),
    "grib_dump": (
        "eccodes",
        ["grib_to_netcdf", "grib_copy", "grib_filter", "grib_ls", "grib_dump"],
    ),
    "ncks": (
        "nco",
        ["ncks", "ncrcat", "ncap2", "ncatted", "ncra", "ncea", "ncpdq", "ncwa", "ncdiff"],
    ),
    "ncrcat": (
        "nco",
        ["ncks", "ncrcat", "ncap2", "ncatted", "ncra", "ncea", "ncpdq", "ncwa", "ncdiff"],
    ),
    "ncap2": (
        "nco",
        ["ncks", "ncrcat", "ncap2", "ncatted", "ncra", "ncea", "ncpdq", "ncwa", "ncdiff"],
    ),
    "ncatted": (
        "nco",
        ["ncks", "ncrcat", "ncap2", "ncatted", "ncra", "ncea", "ncpdq", "ncwa", "ncdiff"],
    ),
    "ncra": (
        "nco",
        ["ncks", "ncrcat", "ncap2", "ncatted", "ncra", "ncea", "ncpdq", "ncwa", "ncdiff"],
    ),
    "ncea": (
        "nco",
        ["ncks", "ncrcat", "ncap2", "ncatted", "ncra", "ncea", "ncpdq", "ncwa", "ncdiff"],
    ),
    "ncpdq": (
        "nco",
        ["ncks", "ncrcat", "ncap2", "ncatted", "ncra", "ncea", "ncpdq", "ncwa", "ncdiff"],
    ),
    "ncwa": (
        "nco",
        ["ncks", "ncrcat", "ncap2", "ncatted", "ncra", "ncea", "ncpdq", "ncwa", "ncdiff"],
    ),
    "ncdiff": (
        "nco",
        ["ncks", "ncrcat", "ncap2", "ncatted", "ncra", "ncea", "ncpdq", "ncwa", "ncdiff"],
    ),
    "ncdump": ("libnetcdf", ["ncdump", "ncgen"]),
    "ncgen": ("libnetcdf", ["ncdump", "ncgen"]),
    "gdal_translate": ("gdal", ["gdal_translate", "gdalwarp", "gdalinfo", "ogr2ogr"]),
    "gdalwarp": ("gdal", ["gdal_translate", "gdalwarp", "gdalinfo", "ogr2ogr"]),
    "gdalinfo": ("gdal", ["gdal_translate", "gdalwarp", "gdalinfo", "ogr2ogr"]),
    "ogr2ogr": ("gdal", ["gdal_translate", "gdalwarp", "gdalinfo", "ogr2ogr"]),
}

CommandRunner = Callable[..., subprocess.CompletedProcess]


def run_command(
    cmd: list[str],
    *,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


class RuntimeToolManager:
    """Discover and install CLI tools in the managed meteora-agent environment."""

    def __init__(
        self,
        packages: dict[str, tuple[str, list[str]]] | None = None,
        command_runner: CommandRunner = run_command,
    ) -> None:
        self.packages = packages or RUNTIME_TOOL_PACKAGES
        self.command_runner = command_runner

    def managed_tools_in_command(self, command: str) -> list[str]:
        return [
            tool
            for tool in self.packages
            if re.search(rf"(?<![\w./-]){re.escape(tool)}(?![\w.-])", command)
        ]

    def tools_ready(
        self,
        tools: list[str],
        env: dict[str, str],
    ) -> tuple[bool, list[str], list[dict]]:
        env_bins = [
            (root / "envs" / "meteora-agent" / "bin").resolve() for root in _conda_roots(env)
        ]
        missing: list[str] = []
        verified: list[dict] = []
        for tool in tools:
            path = shutil.which(tool, path=env.get("PATH"))
            if path is None:
                missing.append(tool)
                continue
            resolved = Path(path).resolve()
            if not any(resolved.parent == env_bin for env_bin in env_bins):
                missing.append(tool)
                verified.append({"tool": tool, "path": path, "reason": "not_in_meteora_agent"})
                continue
            verified.append({"tool": tool, "path": path})
        return not missing, missing, verified

    def find_conda_executable(self, env: dict[str, str]) -> str | None:
        value = env.get("CONDA_EXE")
        if value and Path(value).exists():
            return value
        return shutil.which("conda", path=env.get("PATH"))

    @staticmethod
    def conda_root_from_executable(executable: str) -> Path:
        path = Path(executable).resolve()
        return path.parent.parent if path.parent.name == "bin" else Path.home() / "miniconda3"

    def conda_env_exists(self, conda: str, env: dict[str, str]) -> bool:
        try:
            result = self.command_runner([conda, "env", "list"], env=env, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        return any(
            line.split() and line.split()[0] == "meteora-agent"
            for line in result.stdout.splitlines()
        )

    async def conda_env_exists_async(self, conda: str, env: dict[str, str]) -> bool:
        return await asyncio.to_thread(self.conda_env_exists, conda, env)

    async def run_command_async(
        self,
        cmd: list[str],
        *,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess:
        return await asyncio.to_thread(self.command_runner, cmd, env=env, timeout=timeout)


_DEFAULT_MANAGER = RuntimeToolManager()


def get_runtime_tool_manager() -> RuntimeToolManager:
    return _DEFAULT_MANAGER
