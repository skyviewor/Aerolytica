"""A deleted latest context must never resurrect queued historical archives."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from aero.application.chat_presence import ChatPresence
from aero.application.cloud_runtime import CloudAgentRuntime


@pytest.mark.asyncio
async def test_foreground_reset_discards_durable_archives_and_only_puts_latest(tmp_path):
    presence = ChatPresence.__new__(ChatPresence)
    presence.agent_id = "agent"
    presence.token = "token"
    presence.context_sync_enabled = True
    presence._context_version = 5
    presence._reported_context_version = 6  # Server clear advanced its version/epoch.
    presence._context_hash = "old"
    presence._sync_blocked_version = 5
    presence._pending_compactions = [{"event_id": "old", "before": {}, "after": {}}]
    presence._pending_path = tmp_path / "compactions.json"
    presence._save_pending()
    presence.is_idle = lambda: True
    payload = {"messages": [{"role": "user", "content": "current stable context"}]}
    presence.export_context = lambda: payload
    presence._http = AsyncMock()
    presence._http.get.return_value = httpx.Response(
        404, request=httpx.Request("GET", "https://relay.test/context")
    )
    presence._http.put.return_value = httpx.Response(
        200, request=httpx.Request("PUT", "https://relay.test/context"), json={"version": 7}
    )
    await presence._sync_context()
    assert json.loads(presence._pending_path.read_text()) == []
    presence._http.post.assert_not_called()  # In particular: no context/compact.
    body = presence._http.put.await_args.kwargs["json"]
    assert body["expected_version"] == 6
    assert body["context"] == payload
    assert presence.context_sync_enabled


@pytest.mark.asyncio
async def test_resident_reset_discards_durable_archives_and_only_puts_latest(tmp_path):
    payload = {"messages": [{"role": "user", "content": "current"}]}
    session = SimpleNamespace(
        id="session",
        project_dir=tmp_path,
        metadata=lambda: {"active_runs": []},
        session_manager=SimpleNamespace(export_portable_context=lambda _: payload),
    )
    calls = []

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            response = httpx.Response(
                404, request=httpx.Request("GET", "https://relay.test/context")
            )
            response.raise_for_status()
        return {"version": 7}

    transport = SimpleNamespace(agent_id="agent", request=request)
    runtime = CloudAgentRuntime(
        session,
        transport,
        device_id="device",
        project_id="project",
        account_id="user",
        account_identity=lambda: "user",
        state_dir=tmp_path / "runtime",
    )
    runtime.context_sync_enabled = True
    runtime.context_version = 5
    runtime._reported_context_version = 6
    runtime._pending_compactions = [{"event_id": "old", "before": {}, "after": {}}]
    await runtime._sync_context()
    assert json.loads(runtime._context_events_path.read_text()) == []
    assert [(method, path) for method, path, _ in calls] == [("GET", "context"), ("PUT", "context")]
    assert calls[-1][2]["json"]["expected_version"] == 6
    assert runtime.context_sync_enabled
