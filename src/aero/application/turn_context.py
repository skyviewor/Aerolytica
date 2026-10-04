"""Trusted turn identity, inherited by execution tasks (never supplied by tools)."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_turn_id: ContextVar[str] = ContextVar("aero_turn_id", default="")
_primary_model: ContextVar[tuple[str, str] | None] = ContextVar("aero_primary_model", default=None)


def current_primary_model() -> tuple[str, str] | None:
    return _primary_model.get()


@contextmanager
def use_primary_model(provider: str, model: str):
    """Pass the active session selection to tools, without changing saved settings."""
    token = _primary_model.set((provider, model))
    try:
        yield
    finally:
        _primary_model.reset(token)


def current_turn_id() -> str:
    return _turn_id.get()


@contextmanager
def use_turn_id(turn_id: str):
    if (
        not turn_id
        or len(turn_id) > 64
        or not turn_id.isascii()
        or any(ord(c) < 33 or ord(c) == 127 for c in turn_id)
    ):
        raise ValueError("invalid_relay_turn_id")
    token = _turn_id.set(turn_id)
    try:
        yield
    finally:
        _turn_id.reset(token)
