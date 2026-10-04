"""Owner-private process inventory; file locks, not remembered PIDs, prove life."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from aero.distribution import (
    DistributionError,
    install_root,
    managed_install,
    private_dir,
    write_state,
)
from aero.release_environment import user_home


def _check_barrier() -> None:
    path = install_root() / "updating.json"
    if path.exists():
        state = json.loads(path.read_text())
        if not state.get("token") or state["token"] != os.environ.get("AERO_UPDATE_RESTART_TOKEN"):
            raise DistributionError("客户端正在更新，请稍后重新打开。")


@contextmanager
def process_record(kind: str = "foreground", **control):
    if not managed_install():
        yield
        return
    _check_barrier()
    directory = private_dir(install_root() / "processes")
    key = str(os.getpid())
    lock = directory / f"{key}.lock"
    record = directory / f"{key}.json"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        write_state(
            record,
            {
                "pid": os.getpid(),
                "kind": kind,
                "control": control,
                "environment": str(Path(sys.prefix).resolve()),
                "cwd": str(Path.cwd()),
                "argv": sys.argv[1:],
            },
        )
        _check_barrier()  # Close the start/enumeration race.
        yield
    finally:
        record.unlink(missing_ok=True)
        os.close(fd)
        # Keep the inode: unlinking a held/reopened lock can split ownership.


def lock_is_live(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def live_records(*, exclude_pid: int | None = None) -> list[dict]:
    records = []
    for path in (install_root() / "processes").glob("*.json"):
        if lock_is_live(path.with_suffix(".lock")):
            state = json.loads(path.read_text())
            if state["pid"] != exclude_pid:
                records.append(state)
    # Older runtimes have no lifecycle protocol. Refuse instead of killing.
    agents = {row["control"].get("agent_id") for row in records if row["kind"] == "agent"}
    daemons = {row["control"].get("state_dir") for row in records if row["kind"] == "daemon"}
    for path in (user_home() / "agent-locks").glob("*.lock"):
        if path.stem not in agents and lock_is_live(path):
            raise DistributionError("检测到旧版或前台 Agent，请先在原终端停止，再更新。")
    for path in (user_home() / "daemons").glob("*/daemon.lock"):
        if str(path.parent) not in daemons and lock_is_live(path):
            raise DistributionError("检测到旧版本地服务，请先运行 aero service stop。")
    if any(row["kind"] == "foreground" for row in records):
        raise DistributionError("其他终端仍打开着 Aero；请先退出这些界面，再更新。")
    known = {record["pid"] for record in records} | {os.getpid(), exclude_pid}
    if legacy_processes(known):
        raise DistributionError("检测到未登记的旧版 Aero 进程，请先手动关闭/停止后再更新。")
    return records


def legacy_processes(known: set) -> list[int]:
    """Read-only legacy detection, including custom daemon state directories.

    Never log process arguments: they may include sensitive user input.
    Matching only blocks an update, and is never used to send a signal.
    """
    try:
        result = subprocess.run(
            ["ps", "-axo", "uid=,pid=,args="], capture_output=True, text=True, check=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DistributionError("无法检查本机进程，未执行版本切换。") from exc
    pattern = re.compile(
        r"(?:/|\b)aero\s+(?:chat|serve|agent\s+(?:run|start))\b|"
        r"aero\.cli\.(?:agent_background|daemon)\b"
    )
    unknown = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        uid, pid = int(fields[0]), int(fields[1])
        if uid == os.getuid() and pid not in known and pattern.search(fields[2]):
            unknown.append(pid)
    return unknown
