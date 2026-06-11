"""Lightweight progress reporting from tools back to the agent loop."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass
class ProgressReporter:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[str]
    cancel_event: asyncio.Event | None = None

    def emit(self, message: str) -> None:
        text = str(message).strip()
        if text:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, text)

    def cancel_requested(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())


_current_reporter: ContextVar[ProgressReporter | None] = ContextVar(
    "meteora_progress_reporter",
    default=None,
)


@contextmanager
def use_progress_reporter(reporter: ProgressReporter) -> Iterator[None]:
    token = _current_reporter.set(reporter)
    try:
        yield
    finally:
        _current_reporter.reset(token)


def emit_progress(message: str) -> None:
    reporter = _current_reporter.get()
    if reporter is not None:
        reporter.emit(message)


def cancel_requested() -> bool:
    reporter = _current_reporter.get()
    return bool(reporter and reporter.cancel_requested())
