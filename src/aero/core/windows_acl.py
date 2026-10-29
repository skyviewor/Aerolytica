"""Restrict private user data directories to the current Windows identity."""

from __future__ import annotations

import csv
import os
import subprocess
from pathlib import Path


def _system_tool(name: str) -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / name)


def protect_private_directory(path: Path) -> None:
    if os.name != "nt":
        return
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("unsafe_private_directory")
    user = subprocess.run(
        [_system_tool("whoami.exe"), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
    )
    rows = list(csv.reader(user.stdout.splitlines()))
    if len(rows) != 1 or len(rows[0]) < 2 or not rows[0][1].startswith("S-1-"):
        raise RuntimeError("无法确认当前 Windows 用户")
    sid = rows[0][1]
    icacls = _system_tool("icacls.exe")
    subprocess.run(
        [icacls, str(path), "/grant:r", f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        [icacls, str(path), "/inheritance:r"],
        capture_output=True,
        text=True,
        check=True,
    )
