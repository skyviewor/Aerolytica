"""User instructions — natural-language preferences persisted as Markdown."""

from __future__ import annotations

from pathlib import Path

GLOBAL_INSTRUCTIONS_PATH = Path.home() / ".meteora" / "instructions.md"
PROJECT_INSTRUCTIONS_FILENAME = "AGENTS.md"

MAX_CHAR_LENGTH = 2000


def load_instructions(project_dir: str | Path | None = None) -> str:
    global_text = ""
    if GLOBAL_INSTRUCTIONS_PATH.exists():
        global_text = GLOBAL_INSTRUCTIONS_PATH.read_text(encoding="utf-8").strip()

    project_text = ""
    if project_dir:
        project_path = Path(project_dir) / PROJECT_INSTRUCTIONS_FILENAME
        if project_path.exists():
            project_text = project_path.read_text(encoding="utf-8").strip()

    return _render_section(global_text, project_text)


def _render_section(global_text: str, project_text: str) -> str:
    if not global_text and not project_text:
        return ""

    global_text = global_text[:MAX_CHAR_LENGTH]
    project_text = project_text[:MAX_CHAR_LENGTH]

    parts = []
    if global_text:
        parts.append(f"### 全局偏好\n{global_text}")
    if project_text:
        parts.append(f"### 项目要求\n{project_text}")
    return "\n\n".join(parts)


def _resolve_scope_path(
    scope: str,
    project_dir: str | Path | None = None,
) -> Path:
    if scope == "global":
        return GLOBAL_INSTRUCTIONS_PATH
    if scope == "project":
        project_dir = Path(project_dir) if project_dir else Path.cwd()
        return project_dir / PROJECT_INSTRUCTIONS_FILENAME
    raise ValueError(f"unknown scope: {scope}. Use 'global' or 'project'.")


def append_instruction(
    instruction: str,
    scope: str = "project",
    project_dir: str | Path | None = None,
) -> Path:
    inst = instruction.strip()
    if not inst:
        raise ValueError("instruction must not be empty")

    path = _resolve_scope_path(scope, project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    existing_lines = [l for l in existing.splitlines() if l.strip()]
    existing_lines.append(f"- {inst}")
    path.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")
    return path


def write_instructions(
    text: str,
    scope: str = "project",
    project_dir: str | Path | None = None,
) -> Path:
    trimmed = text.strip()
    path = _resolve_scope_path(scope, project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(trimmed + "\n" if trimmed else "", encoding="utf-8")
    return path


def clear_instructions(
    scope: str = "project",
    project_dir: str | Path | None = None,
) -> Path:
    path = _resolve_scope_path(scope, project_dir)
    if path.exists():
        path.unlink()
    return path
