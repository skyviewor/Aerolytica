"""One cross-process lease for Agent file work and cloud synchronization."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from aero.core.daemon import WorkspaceLock

_owned = ContextVar("aero_project_execution", default={})


@asynccontextmanager
async def project_execution(root: Path, *, only_if_sync_configured: bool = False):
    root = Path(root).resolve()
    existing = _owned.get().get(root)
    if existing and existing[0]._fd is not None:
        existing[1] += 1
        try:
            yield
        finally:
            existing[1] -= 1
            if existing[1] == 0:
                existing[0].release()
        return
    if only_if_sync_configured and not (root / ".aero/cloud-sync/state.json").exists():
        yield
        return
    lock = WorkspaceLock(root / ".aero/runtime/execution.lock")
    while True:
        try:
            lock.acquire()
            break
        except BlockingIOError:
            await asyncio.sleep(0.1)
    shared = [lock, 1]
    token = _owned.set({**_owned.get(), root: shared})
    try:
        yield
    finally:
        _owned.reset(token)
        shared[1] -= 1
        if shared[1] == 0:
            lock.release()
