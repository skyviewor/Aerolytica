"""Authorization channel, deliberately independent from ordinary input forms."""

from contextlib import contextmanager
from contextvars import ContextVar

_provider = ContextVar("aero_approval_provider", default=None)


@contextmanager
def use_approval_provider(provider):
    token = _provider.set(provider)
    try:
        yield
    finally:
        _provider.reset(token)


async def request_approval_from_context(payload: dict) -> str:
    provider = _provider.get()
    return await provider(payload) if provider else "deny"
