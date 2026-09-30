"""Secure, UI-mediated secret entry for tool calls.

The model receives only a one-time handle. Secret text stays in the client
process and is resolved locally by the configuration tool that consumes it.
"""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretInputRequest:
    scope: str
    title: str
    instructions: str
    multiline: bool = False


SecretConsumer = Callable[[str], dict]


@dataclass(frozen=True)
class CredentialSpec:
    """Declarative contract for a capability-owned credential.

    Adding a credential type means registering one of these specs.  The UI
    never needs a per-provider branch: it renders the request and forwards the
    opaque handle to the registered local consumer.
    """

    scope: str
    title: str
    instructions: str
    multiline: bool
    consumer: SecretConsumer


_CREDENTIAL_SPECS: dict[str, CredentialSpec] = {}


def register_credential_spec(spec: CredentialSpec) -> None:
    """Register the local parser/saver for a credential scope."""
    _CREDENTIAL_SPECS[spec.scope] = spec


def get_credential_spec(scope: str) -> CredentialSpec | None:
    """Return a known credential contract without exposing any secret."""
    return _CREDENTIAL_SPECS.get(scope)


def credential_request_for(scope: str) -> dict:
    """Build the serializable request a capability returns when setup is needed."""
    spec = get_credential_spec(scope)
    if spec is None:
        return {"scope": scope, "sensitive": True}
    return {
        "scope": spec.scope,
        "title": spec.title,
        "instructions": spec.instructions,
        "multiline": spec.multiline,
        "sensitive": True,
    }


def save_secret_from_context(scope: str, handle: str) -> dict:
    """Consume one opaque handle with the scope's registered local saver."""
    spec = get_credential_spec(scope)
    if spec is None:
        return {"status": "error", "message": f"未知的凭据用途：{scope}"}
    secret = take_secret_from_context(handle)
    if not secret:
        return {
            "status": "error",
            "message": "未收到有效的安全凭据句柄，请重新打开安全配置窗口。",
        }
    if isinstance(secret, dict):
        return secret["result"] if secret.get("scope") == scope else {"status": "error"}
    return spec.consumer(secret)


SecretInputProvider = Callable[[SecretInputRequest], Awaitable[dict]]
SecretHandleResolver = Callable[[str], str | dict | None]

_SECRET_INPUT_PROVIDER: contextvars.ContextVar[SecretInputProvider | None] = contextvars.ContextVar(
    "aero_secret_input_provider", default=None
)
_SECRET_HANDLE_RESOLVER: contextvars.ContextVar[SecretHandleResolver | None] = (
    contextvars.ContextVar("aero_secret_handle_resolver", default=None)
)


@contextmanager
def use_secret_input_provider(provider: SecretInputProvider, resolver: SecretHandleResolver):
    provider_token = _SECRET_INPUT_PROVIDER.set(provider)
    resolver_token = _SECRET_HANDLE_RESOLVER.set(resolver)
    try:
        yield
    finally:
        _SECRET_HANDLE_RESOLVER.reset(resolver_token)
        _SECRET_INPUT_PROVIDER.reset(provider_token)


async def request_secret_input_from_context(request: SecretInputRequest) -> dict:
    provider = _SECRET_INPUT_PROVIDER.get()
    if provider is None:
        return {
            "status": "unavailable",
            "message": "当前运行环境无法打开安全凭据输入窗口。",
        }
    return await provider(request)


def take_secret_from_context(handle: str) -> str | dict | None:
    resolver = _SECRET_HANDLE_RESOLVER.get()
    return resolver(handle) if resolver is not None else None
