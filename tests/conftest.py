"""Hermetic client tests: no real account, user state, or public-network requests."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

UV_CACHE = os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache/uv"))


@pytest.fixture(autouse=True)
def isolated_user_state(monkeypatch, tmp_path_factory):
    home = tmp_path_factory.mktemp("aero-user")
    monkeypatch.chdir(tmp_path_factory.mktemp("aero-workspace"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("UV_CACHE_DIR", UV_CACHE)
    for variable in (
        "AERO_HOME",
        "AERO_SECRETS_PATH",
        "AERO_ENVIRONMENT",
        "AERO_INSTALL_ROOT",
        "AERO_RUNTIME_ROOT",
        "AERO_RUNTIME_ENV",
        "AERO_MICROMAMBA",
        "AERO_OFFICIAL_API_URL",
        "AERO_OFFICIAL_LLM_URL",
        "AERO_DOWNLOAD_BASE",
    ):
        monkeypatch.delenv(variable, raising=False)
    from aero.data import instructions

    monkeypatch.setattr(instructions, "GLOBAL_INSTRUCTIONS_PATH", home / ".aero/instructions.md")

    sync_send = httpx.HTTPTransport.handle_request
    async_send = httpx.AsyncHTTPTransport.handle_async_request

    def check(request):
        if request.url.host not in {"localhost", "127.0.0.1", "::1"}:
            raise AssertionError("Tests must mock external HTTP requests")

    def send(transport, request):
        check(request)
        return sync_send(transport, request)

    async def send_async(transport, request):
        check(request)
        return await async_send(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send_async)


@pytest.fixture
def byok_ui(monkeypatch):
    """Exercise retained BYOK implementations without enabling them in official releases."""
    from aero.cli import main, setup_wizard
    from aero.core import llm_providers

    monkeypatch.setattr(llm_providers, "BYOK_UI_ENABLED", True)
    monkeypatch.setattr(main, "BYOK_UI_ENABLED", True)
    monkeypatch.setattr(setup_wizard, "BYOK_UI_ENABLED", True)
