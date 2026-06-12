"""Background debug logging for Aero.

This module writes JSONL debug events to a local file. It is intentionally
decoupled from the TUI so debug logs never appear on screen.
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_BACKUPS = 3

_lock = threading.Lock()
_enabled = False
_path: Path | None = None
_max_bytes = _DEFAULT_MAX_BYTES
_backups = _DEFAULT_BACKUPS


def configure_debug_logging(
    *,
    enabled: bool | None = None,
    path: str | Path | None = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backups: int = _DEFAULT_BACKUPS,
) -> Path:
    """Configure the background debug log and return the active path."""
    global _enabled, _path, _max_bytes, _backups

    env_enabled = os.environ.get("AERO_DEBUG_LOG")
    if enabled is None:
        enabled = env_enabled != "0"

    env_path = os.environ.get("AERO_DEBUG_LOG_PATH")
    log_path = Path(path or env_path or Path.home() / ".aero" / "debug.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with _lock:
        _enabled = enabled
        _path = log_path
        _max_bytes = max_bytes
        _backups = backups

    debug_log(
        "debug_log.configured",
        enabled=enabled,
        path=str(log_path),
        max_bytes=max_bytes,
        backups=backups,
    )
    return log_path


def debug_log_path() -> Path:
    """Return the configured debug log path, configuring defaults if needed."""
    if _path is None:
        return configure_debug_logging()
    return _path


def debug_log(event: str, **fields: Any) -> None:
    """Append a single debug event.

    Logging errors are swallowed so diagnostics never break the app.
    """
    try:
        if not _enabled or _path is None:
            return
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            **_json_safe(fields),
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with _lock:
            _rotate_if_needed(_path)
            with _path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
    except Exception:
        return


def debug_exception(event: str, exc: BaseException, **fields: Any) -> None:
    """Append a debug event for a caught exception, including traceback text."""
    debug_log(
        event,
        exception_type=type(exc).__name__,
        error=str(exc),
        error_repr=repr(exc),
        traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        **fields,
    )


def _rotate_if_needed(path: Path) -> None:
    if _max_bytes <= 0 or not path.exists() or path.stat().st_size < _max_bytes:
        return
    for index in range(_backups - 1, 0, -1):
        src = path.with_suffix(path.suffix + f".{index}")
        dst = path.with_suffix(path.suffix + f".{index + 1}")
        if src.exists():
            src.replace(dst)
    first_backup = path.with_suffix(path.suffix + ".1")
    path.replace(first_backup)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
