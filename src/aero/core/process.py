"""Cross-platform process group creation and cancellation."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess


def isolated_process_kwargs() -> dict[str, int | bool]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def terminate_process_tree(process: asyncio.subprocess.Process, grace: float = 3) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            await asyncio.wait_for(process.wait(), timeout=grace)
            return
        except (OSError, asyncio.TimeoutError):
            # taskkill /T is required to include children of cmd.exe and scripts.
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
            await process.wait()
            return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
