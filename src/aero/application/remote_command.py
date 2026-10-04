"""Command-scoped settings; application entry points, not tools, own overrides."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_command: ContextVar[dict | None] = ContextVar("aero_remote_command", default=None)


def current_command() -> dict:
    return _command.get() or {}


@contextmanager
def use_remote_command(command: dict):
    token = _command.set(command)
    try:
        yield
    finally:
        _command.reset(token)


@contextmanager
def command_settings(agent):
    command = current_command()
    model = command.get("model")
    mode = command.get("mode")
    if mode is not None and mode not in {"execute", "plan", "qa"}:
        raise ValueError("invalid_command_mode")
    if model is not None and not isinstance(model, str):
        raise ValueError("invalid_command_model")
    original = (agent.config.llm.model, agent.llm.config.model, agent.config.mode)
    try:
        if model:
            selected = "auto" if model in {"auto", "default"} else model
            agent.config.llm.model = selected
            agent.llm.config.model = selected
        if mode:
            agent.config.mode = mode
        yield
    finally:
        agent.config.llm.model, agent.llm.config.model, agent.config.mode = original
