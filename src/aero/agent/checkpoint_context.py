"""Context bridge for checkpoint creation from an active chat session."""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from contextlib import contextmanager

CheckpointCreator = Callable[[str], dict]

_CHECKPOINT_CREATOR: contextvars.ContextVar[CheckpointCreator | None] = contextvars.ContextVar(
    "aero_checkpoint_creator", default=None
)


@contextmanager
def use_checkpoint_creator(creator: CheckpointCreator):
    token = _CHECKPOINT_CREATOR.set(creator)
    try:
        yield
    finally:
        _CHECKPOINT_CREATOR.reset(token)


def create_checkpoint_from_context(name: str) -> dict:
    creator = _CHECKPOINT_CREATOR.get()
    if creator is None:
        return {
            "success": False,
            "error": "当前运行环境不能保存对话检查点，请使用 /checkpoint。",
        }
    return creator(name)
