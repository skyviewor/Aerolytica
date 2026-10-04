"""Monotonic execution timers; queue wait and fee settlement are not execution."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class RequestTimer:
    started: float | None = None
    ended: float | None = None

    def start(self) -> None:
        if self.started is None:
            self.started = time.monotonic()

    def stop(self) -> None:
        if self.started is not None and self.ended is None:
            self.ended = time.monotonic()

    def payload(self) -> dict:
        if self.started is None:
            return {}
        end = self.ended if self.ended is not None else time.monotonic()
        return {"execution_ms": max(0, round((end - self.started) * 1000))}


_timer: ContextVar[RequestTimer | None] = ContextVar("request_timer", default=None)


@contextmanager
def timed_request():
    timer = RequestTimer()
    token = _timer.set(timer)
    try:
        yield timer
    finally:
        timer.stop()
        _timer.reset(token)


def start_execution_timer() -> None:
    timer = _timer.get()
    if timer is not None:
        timer.start()
