"""Shared state and safety checks for local file tools."""

from pathlib import Path

from meteora.toolbox.paths import find_project_dir

READ_FILES: set[str] = set()


def is_in_project(path: Path) -> bool:
    project_dir = find_project_dir()
    try:
        path.resolve().relative_to(project_dir.resolve())
        return True
    except ValueError:
        return False
