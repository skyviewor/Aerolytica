"""Project path helpers shared by domain tool modules."""

from pathlib import Path


def find_project_dir() -> Path:
    """Return the current Aero workspace directory."""
    return Path.cwd()


def resolve_project_path(path: str | Path) -> Path:
    """Resolve relative paths from the project root, not from transient cwd."""
    value = Path(path)
    if value.is_absolute():
        return value
    return find_project_dir() / value


def short_path(path: str | Path) -> str:
    """Return a project-relative path when possible."""
    try:
        return str(Path(path).relative_to(find_project_dir()))
    except ValueError:
        return str(path)
