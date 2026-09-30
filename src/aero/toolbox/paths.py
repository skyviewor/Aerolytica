"""Project path helpers shared by domain tool modules."""

import contextvars
from contextlib import contextmanager
from pathlib import Path

_PROJECT_DIR: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "aero_project_dir", default=None
)
_WORKSPACE_DIR: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "aero_workspace_dir", default=None
)


def find_project_dir() -> Path:
    """Return the project root opened by the user."""
    return _PROJECT_DIR.get() or Path.cwd()


def find_workspace_dir() -> Path:
    """Return the active writable workspace, or the project root."""
    return _WORKSPACE_DIR.get() or find_project_dir()


@contextmanager
def use_workspace(project_dir: str | Path, workspace_dir: str | Path | None = None):
    """Bind project and writable workspace paths for one Agent run."""
    project = Path(project_dir).resolve()
    workspace = Path(workspace_dir).resolve() if workspace_dir else project
    project_token = _PROJECT_DIR.set(project)
    workspace_token = _WORKSPACE_DIR.set(workspace)
    try:
        yield
    finally:
        _WORKSPACE_DIR.reset(workspace_token)
        _PROJECT_DIR.reset(project_token)


def resolve_project_path(path: str | Path) -> Path:
    """Resolve relative paths from the active writable workspace."""
    value = Path(path)
    if value.is_absolute():
        return value.expanduser().resolve()
    return (find_workspace_dir() / value.expanduser()).resolve()


def short_path(path: str | Path) -> str:
    """Return a project-relative path when possible."""
    try:
        return str(Path(path).relative_to(find_project_dir()))
    except ValueError:
        return str(path)
