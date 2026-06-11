"""Plan document management — timestamped .md files with state tracking.

Each session gets its own subdirectory under plans/ to avoid cross-session
collisions: plans/<session_id>/plan_20250608_1430_01.md
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

PLANS_DIR_NAME = "plans"
STATE_FILE = "_plan_state.json"
FILENAME_PREFIX = "plan"

_current_session_id: str | None = None


def set_session_id(session_id: str | None) -> None:
    """Set the current session id so plans are written under plans/<session_id>/.

    Called by the CLI layer whenever a session is created, loaded, or cleared.
    """
    global _current_session_id
    _current_session_id = session_id


def get_session_id() -> str | None:
    """Return the currently active session id, or None."""
    return _current_session_id


def _plans_dir(
    project_dir: str | Path | None = None,
    session_id: str | None = None,
) -> Path:
    base = Path(project_dir) if project_dir else Path.cwd()
    sid = session_id if session_id is not None else _current_session_id
    if sid:
        return base / PLANS_DIR_NAME / sid
    return base / PLANS_DIR_NAME


def _state_path(
    project_dir: str | Path | None = None,
    session_id: str | None = None,
) -> Path:
    return _plans_dir(project_dir, session_id=session_id) / STATE_FILE


def load_state(
    project_dir: str | Path | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    sp = _state_path(project_dir, session_id=session_id)
    if not sp.exists():
        return {}
    try:
        with open(sp) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return {}


def _save_state(
    data: dict[str, Any],
    project_dir: str | Path | None = None,
    session_id: str | None = None,
) -> None:
    sp = _state_path(project_dir, session_id=session_id)
    sp.parent.mkdir(parents=True, exist_ok=True)
    with open(sp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def lock_current_plan(
    project_dir: str | Path | None = None,
    session_id: str | None = None,
) -> None:
    state = load_state(project_dir, session_id=session_id)
    if state.get("current_plan") and not state.get("locked"):
        state["locked"] = True
        _save_state(state, project_dir, session_id=session_id)


def is_plan_locked(
    project_dir: str | Path | None = None,
    session_id: str | None = None,
) -> bool:
    return bool(load_state(project_dir, session_id=session_id).get("locked", False))


def _slugify_title(title: str, max_len: int = 40) -> str:
    slug = re.sub(r'[\\/:*?"<>|]', "", title)
    slug = re.sub(r"[\s_]+", "_", slug).strip("_.")
    slug = re.sub(r"[^\w\-]", "", slug)
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("_")
    return slug


def resolve_plan_path(
    project_dir: str | Path | None = None,
    session_id: str | None = None,
) -> Path:
    state = load_state(project_dir, session_id=session_id)
    if state.get("current_plan") and not state.get("locked"):
        path = Path(state["current_plan"])
        if path.exists():
            return path
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M")
    seq = state.get("plan_sequence", 0) + 1
    filename = f"{FILENAME_PREFIX}_{timestamp}_{seq:02d}.md"
    path = _plans_dir(project_dir, session_id=session_id) / filename
    _save_state({
        "current_plan": str(path),
        "locked": False,
        "plan_sequence": seq,
    }, project_dir, session_id=session_id)
    return path


def write_plan(
    content: str,
    title: str = "",
    project_dir: str | Path | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    trimmed = content.strip()
    if not trimmed:
        raise ValueError("plan content must not be empty")

    plan_path = resolve_plan_path(project_dir, session_id=session_id)
    plan_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    if title:
        lines.append(f"# {title}\n")
    lines.append(trimmed)
    lines.append("")
    plan_path.write_text("\n".join(lines), encoding="utf-8")

    state = load_state(project_dir, session_id=session_id)
    return {
        "saved_to": str(plan_path),
        "title": title or "未命名计划",
        "locked": state.get("locked", False),
        "size_bytes": plan_path.stat().st_size,
    }


def list_plan_sessions(
    project_dir: str | Path | None = None,
) -> list[str]:
    """List session IDs that have plan directories under plans/.

    Returns session IDs (subdirectory names) sorted alphabetically.
    """
    plans_root = _plans_dir(project_dir, session_id="")
    if not plans_root.exists():
        return []
    sessions: list[str] = []
    for entry in sorted(plans_root.iterdir()):
        if entry.is_dir() and (entry / STATE_FILE).exists():
            sessions.append(entry.name)
    return sessions
