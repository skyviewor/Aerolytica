"""Account capacity queries use the official API, never real credentials."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from aero.core.cloud_files import CloudFiles
from aero.data.modes import is_tool_allowed
from aero.toolbox.registry import get_registry
from aero.toolbox.tools import cloud


@pytest.mark.parametrize(
    "change,available",
    [
        ({}, 650),
        ({"used_bytes": 1000, "reserved_bytes": 100, "over_quota": True}, 0),
        ({"limit_bytes": 0, "enabled": False}, 0),
        ({"read_only": True}, 0),
        ({"limit_bytes": 2000, "base_limit_bytes": 1000, "expansion_bytes": 1000}, 1650),
    ],
)
async def test_capacity_formula_and_accounting_scope(change, available):
    payload = {
        "limit_bytes": 1000,
        "used_bytes": 300,
        "reserved_bytes": 50,
        "enabled": True,
        "history_bytes": 80,
        "file_bytes": 200,
        "agent_context_bytes": 100,
        "agent_contexts": [{"name": "private conversation"}],
        "download_url": "private signed url",
        **change,
    }
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        session = SimpleNamespace(
            request=lambda method, path, **kw: http.request(
                method, "https://relay.test" + path, **kw
            )
        )
        result = await CloudFiles(session).quota()
    assert result["available_bytes"] == available
    assert result["used_bytes"] == payload["used_bytes"]
    assert result["scope"] == "account"
    assert result["history_bytes"] == 80
    assert "private" not in str(result)
    assert len(calls) == 1 and calls[0].method == "GET"
    assert calls[0].url.path == "/v1/storage/v2/quota" and not calls[0].url.query


@pytest.mark.parametrize(
    "change",
    [
        {"limit_bytes": None},
        {"used_bytes": -1},
        {"reserved_bytes": True},
        {"used_bytes": "300"},
        {"history_bytes": 1.5},
        {"enabled": "false"},
    ],
)
async def test_malformed_capacity_is_not_reported_as_free_space(change):
    files = CloudFiles(None)
    files.api = AsyncMock(
        return_value={
            "limit_bytes": 1000,
            "used_bytes": 300,
            "reserved_bytes": 50,
            "enabled": True,
            **change,
        }
    )
    with pytest.raises(ValueError):
        await files.quota()


async def test_tool_authentication_readonly_modes_and_shared_session(monkeypatch):
    import aero.toolbox.builtin_tools  # noqa: F401

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "limit_bytes": 1024,
                "used_bytes": 0,
                "reserved_bytes": 0,
                "enabled": True,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        session = SimpleNamespace(
            access_token=AsyncMock(return_value="private-account-token"),
            close=AsyncMock(),
            request=lambda method, path, **kw: http.request(
                method, "https://relay.test" + path, **kw
            ),
        )
        monkeypatch.setattr(cloud, "OfficialAccountSession", lambda: session)
        result = await cloud.get_cloud_storage_quota()
    assert result["status"] == "success" and result["result"]["available_bytes"] == 1024
    assert "private" not in str(result)
    session.access_token.assert_awaited_once()
    session.close.assert_awaited_once()
    assert len(requests) == 1
    spec = get_registry().get("get_cloud_storage_quota")
    assert spec is not None and not spec.requires_confirmation
    for mode in ("execute", "plan", "qa"):
        assert is_tool_allowed(spec.name, mode)
