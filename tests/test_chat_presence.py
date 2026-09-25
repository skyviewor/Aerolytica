"""Remote stop and revoked-login behavior for foreground chat presence."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from aero.application.chat_presence import ChatPresence
from aero.core.official_account import OfficialLoginRequiredError


@pytest.mark.asyncio
async def test_remote_stop_blocks_only_current_chat():
    presence = ChatPresence.__new__(ChatPresence)
    presence.agent_id = "agent_1"
    presence.token = "agt_secret"
    presence._active_session_id = "chat-1"
    presence._blocked_session_id = ""
    presence._command_task = None
    presence._exit = AsyncMock()
    presence._http = AsyncMock()
    presence._http.get.return_value = httpx.Response(410)

    await presence._poll_once()

    assert presence._blocked_session_id == "chat-1"
    presence._exit.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoked_login_prompts_after_agent_poll_401():
    presence = ChatPresence.__new__(ChatPresence)
    presence.agent_id = "agent_1"
    presence.token = "agt_secret"
    presence._active_session_id = "chat-1"
    presence._blocked_session_id = ""
    presence._command_task = None
    presence._prompted = False
    presence._exit = AsyncMock()
    presence.on_login_required = AsyncMock()
    presence.session = AsyncMock()
    presence.session.access_token.side_effect = OfficialLoginRequiredError("expired")
    presence._http = AsyncMock()
    presence._http.get.return_value = httpx.Response(401)

    await presence._poll_once()

    presence.session.access_token.assert_awaited_once_with(force_refresh=True)
    presence.on_login_required.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_context_restores_when_local_empty_and_syncs_once():
    presence = ChatPresence.__new__(ChatPresence)
    presence.agent_id = "agent_1"
    presence.token = "agt_secret"
    presence._context_version = 0
    presence._context_hash = ""
    presence.context_sync_enabled = True
    presence._pending_compactions = []
    presence._http = AsyncMock()
    payload = {"schema": 1, "meta": {}, "messages": [{"role": "user", "content": "你好"}]}
    request = httpx.Request("GET", "https://example.test/context")
    presence._http.get.return_value = httpx.Response(200, request=request, json={
        "version": 3, "context": payload,
    })
    presence._http.put.return_value = httpx.Response(
        200, request=httpx.Request("PUT", "https://example.test/context"),
        json={"version": 4},
    )
    presence.export_context = lambda: None
    presence.restore_context = Mock()
    presence.is_idle = lambda: True

    await presence._restore_context()
    assert presence._context_version == 3
    presence.restore_context.assert_called_once_with(payload)

    presence.export_context = lambda: payload
    await presence._sync_context()
    await presence._sync_context()
    assert presence._context_version == 4
    presence._http.put.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_context_does_not_upload_when_sync_disabled():
    presence = ChatPresence.__new__(ChatPresence)
    presence.context_sync_enabled = False
    presence._http = AsyncMock()
    presence.export_context = lambda: {"messages": [{"role": "user", "content": "hello"}]}
    await presence._sync_context()
    presence._http.put.assert_not_called()


@pytest.mark.asyncio
async def test_recovered_oss_append_requires_retry_of_newer_context():
    presence = ChatPresence.__new__(ChatPresence)
    presence.context_sync_enabled = True
    presence._pending_compactions = []
    presence._context_version = 0
    presence._context_hash = ""
    presence.agent_id = "agent_1"
    presence.token = "agt_secret"
    presence.export_context = lambda: {"schema": 1, "meta": {}, "messages": [
        {"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]}
    presence._http = AsyncMock()
    presence._http.put.return_value = httpx.Response(
        200, request=httpx.Request("PUT", "https://example.test/context"),
        json={"version": 1, "applied": False},
    )
    await presence._sync_context()
    assert presence._context_version == 1
    assert presence._context_hash == ""
