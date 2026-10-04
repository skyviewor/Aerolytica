"""One runtime version source, also usable from an uninstalled checkout."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def get_version() -> str:
    try:
        return version("aerolytica")
    except PackageNotFoundError:
        import tomllib

        project = Path(__file__).resolve().parents[2] / "pyproject.toml"
        try:
            data = tomllib.loads(project.read_text(encoding="utf-8"))
            if data["project"]["name"] in {"aerolytica", "aero"}:
                return str(data["project"]["version"])
        except (OSError, ValueError, KeyError):
            pass
        return "unknown"


def title() -> str:
    return f"Aerolytica v{get_version()}"
