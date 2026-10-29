"""Hermetic control/lifecycle tests: no real daemon, model or cloud service."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from aero.agent.loop import AgentLoop
from aero.application.approval_details import approval_payload
from aero.core import sync_control
from aero.core.cloud_sync import CloudSyncEngine, CloudSyncError
from aero.core.config import AeroConfig
from aero.core.daemon import WorkspaceLock
from aero.core.project_execution import project_execution
from aero.core.types import ToolCall
from aero.data.modes import is_tool_allowed
from aero.server.cloud import CloudServices, SyncControlBody, cloud_router
from aero.toolbox.paths import use_workspace


@pytest.fixture
def setup(tmp_path, monkeypatch):
    account = SimpleNamespace(
        data=SimpleNamespace(user_id="u"),
        base_url="https://relay.test",
        access_token=AsyncMock(),
        close=AsyncMock(),
        request=AsyncMock(side_effect=AssertionError("no external calls")),
    )
    monkeypatch.setattr("aero.server.cloud.OfficialAccountSession", lambda: account)
    engine = CloudSyncEngine(tmp_path, account)
    with engine._locked():
        engine._state["binding"] = {
            "user_id": "u",
            "base_url": account.base_url,
            "local_root": str(tmp_path),
            "project_id": "p",
            "root_directory_id": None,
            "includes": ["papers"],
        }
        engine._paused = True
        engine._save()
    engine._remote = AsyncMock(return_value={})
    engine._recover = AsyncMock()
    web = SimpleNamespace(project_dir=tmp_path, active_sessions={})
    services = CloudServices(web)
    services._sync = engine
    web.cloud = services
    return tmp_path, account, engine, services, web


async def test_manual_sync_preserves_paused_setting_and_real_runtime_status(setup):
    root, account, engine, services, _ = setup
    scope = engine.inspect_scope(root)
    result = await services.control_sync(
        SyncControlBody(action="run_once", expected_revision=scope["revision"])
    )
    assert result["status"] == "queued" and result["manual_job"]["state"] == "queued"
    await services._manual_sync_task
    status = services.sync_control_status()
    assert status["manual_job"]["state"] == "completed"
    assert status["configuration"]["paused"] and not status["automatic_task_running"]
    assert status["configuration"]["last_sync_at"]
    engine._remote.assert_awaited_once()
    account.request.assert_not_awaited()
    await services.close()


async def test_busy_workspace_queues_and_deduplicates_one_shot(setup):
    root, _, engine, services, _ = setup
    revision = engine.inspect_scope(root)["revision"]
    body = SyncControlBody(action="run_once", expected_revision=revision)
    async with project_execution(root):
        first = await services.control_sync(body)
        task = services._manual_sync_task
        second = await services.control_sync(body)
        assert first["manual_job"]["job_id"] == second["manual_job"]["job_id"]
        assert services._manual_sync_task is task
        await asyncio.sleep(0)
        assert services._manual_job["state"] == "queued"
        engine._remote.assert_not_awaited()
    await asyncio.wait_for(task, 3)
    assert services._manual_job["state"] == "completed"
    await services.close()


async def test_enable_disable_and_interval(setup):
    root, _, engine, services, _ = setup
    scope = engine.inspect_scope(root)
    result = await services.control_sync(
        SyncControlBody(action="enable", expected_revision=scope["revision"], interval_seconds=90)
    )
    assert result["configuration"]["automatic_enabled"] and result["automatic_task_running"]
    assert result["configuration"]["interval_seconds"] == 90
    result = await services.control_sync(
        SyncControlBody(action="disable", expected_revision=result["configuration"]["revision"])
    )
    assert not result["automatic_task_running"] and result["configuration"]["paused"]
    assert result["configuration"]["interval_seconds"] == 90
    assert not services.agents
    await services.close()


async def test_disable_cancels_queued_manual_request_without_running_it(setup):
    root, _, engine, services, _ = setup
    revision = engine.inspect_scope(root)["revision"]
    async with project_execution(root):
        await services.control_sync(SyncControlBody(action="run_once", expected_revision=revision))
        await asyncio.sleep(0)
        result = await services.control_sync(
            SyncControlBody(action="disable", expected_revision=revision)
        )
    assert result["manual_job"]["state"] == "canceled"
    engine._remote.assert_not_awaited()
    await services.close()


@pytest.mark.parametrize("problem", ["stale", "account", "transport"])
async def test_failures_never_claim_completion(setup, problem):
    root, account, engine, services, _ = setup
    revision = engine.inspect_scope(root)["revision"]
    if problem == "stale":
        with pytest.raises(HTTPException):
            await services.control_sync(
                SyncControlBody(action="run_once", expected_revision="stale")
            )
        assert services._manual_sync_task is None
    elif problem == "account":
        account.data.user_id = "other"
        with pytest.raises(HTTPException):
            await services.control_sync(
                SyncControlBody(action="run_once", expected_revision=revision)
            )
    else:
        engine._remote.side_effect = httpx.ReadTimeout("private-signed-url")
        await services.control_sync(SyncControlBody(action="run_once", expected_revision=revision))
        await services._manual_sync_task
        assert services._manual_job["state"] == "failed"
        assert "private-signed-url" not in str(services.sync_control_status())
    await services.close()


async def test_loopback_control_routes_require_auth_and_validate_body(setup):
    root, _, engine, services, web = setup
    app = FastAPI()

    def require_auth(request: Request):
        if request.headers.get("authorization") != "Bearer test-only":
            raise HTTPException(401)
        return web

    app.include_router(cloud_router(require_auth))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as http:
        assert (await http.get("/api/v1/cloud/sync/control/status")).status_code == 401
        headers = {"Authorization": "Bearer test-only"}
        assert (
            await http.get("/api/v1/cloud/sync/control/status", headers=headers)
        ).status_code == 200
        response = await http.post(
            "/api/v1/cloud/sync/control",
            headers=headers,
            json={
                "action": "enable",
                "expected_revision": engine.inspect_scope(root)["revision"],
                "interval_seconds": 1,
            },
        )
        assert response.status_code == 422
    await services.close()


async def test_status_does_not_spawn_service_and_shutdown_can_be_offline(tmp_path, monkeypatch):
    manager = SimpleNamespace(status=Mock(return_value={"running": False}), start=Mock())
    monkeypatch.setattr(sync_control, "DaemonManager", lambda _: manager)
    session = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(sync_control, "OfficialAccountSession", lambda: session)
    status = await sync_control.control_status(tmp_path)
    assert not status["service_running"] and not (tmp_path / ".aero").exists()
    result = await sync_control.apply_control(
        tmp_path, "disable", status["configuration"]["revision"]
    )
    assert result["configuration"]["paused"]
    manager.start.assert_not_called()


@pytest.mark.parametrize("action", ["enable", "run_once"])
async def test_approved_controls_start_existing_daemon_and_use_private_channel(
    setup, monkeypatch, action
):
    root, _, engine, services, _ = setup
    manager = SimpleNamespace(status=Mock(return_value={"running": False}), start=Mock())
    monkeypatch.setattr(sync_control, "DaemonManager", lambda _: manager)
    request = AsyncMock(return_value={"status": "queued"})
    monkeypatch.setattr(sync_control, "_request", request)
    result = await sync_control.apply_control(root, action, engine.inspect_scope(root)["revision"])
    assert result["status"] == "queued"
    manager.start.assert_called_once()
    assert request.await_args.args[1:3] == ("POST", "/api/v1/cloud/sync/control")
    assert "token" not in str(result)
    await services.close()


async def test_execution_lease_is_reentrant_and_outlives_parent_for_active_child(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def child():
        async with project_execution(tmp_path):
            started.set()
            await release.wait()

    async with project_execution(tmp_path):
        task = asyncio.create_task(child())
        await started.wait()
    competing = WorkspaceLock(tmp_path / ".aero/runtime/execution.lock")
    with pytest.raises(BlockingIOError):
        competing.acquire()
    release.set()
    await task
    competing.acquire()
    competing.release()


@pytest.mark.parametrize("tool", ["set_cloud_sync_enabled", "sync_cloud_now"])
@pytest.mark.parametrize("choice", ["deny", "allow", "unknown"])
async def test_agent_settings_require_fresh_approval(setup, monkeypatch, tool, choice):
    import aero.toolbox.builtin_tools  # noqa: F401

    root, _, engine, services, _ = setup
    apply = AsyncMock(return_value={"status": "queued"})
    monkeypatch.setattr(sync_control, "apply_control", apply)
    config = AeroConfig.create_default()
    config.llm.api_key = "test-only"
    agent = AgentLoop(config)
    agent.always_allow.add(tool)
    args = {"expected_revision": engine.inspect_scope(root)["revision"]}
    if tool == "set_cloud_sync_enabled":
        args["enabled"] = True
    with use_workspace(root):
        count = 0
        async for event in agent._execute_one_tool_stream(
            ToolCall(id="c", name=tool, arguments=args)
        ):
            if event.type == "confirm":
                count += 1
                payload = approval_payload(json.loads(event.content))
                assert "同步目录" in str(payload) and "双向" in str(payload)
                agent.confirm_future.set_result(choice)
    assert count == 1 and apply.await_count == (1 if choice == "allow" else 0)
    for mode in ("plan", "qa"):
        assert not is_tool_allowed(tool, mode)
        assert is_tool_allowed("get_cloud_sync_status", mode)
    await agent.close()
    await services.close()


@pytest.mark.parametrize("interval", [0, 29, 901, True, "30"])
def test_invalid_intervals_and_unbound_enable_rejected(tmp_path, interval):
    revision = CloudSyncEngine.inspect_scope(tmp_path)["revision"]
    with pytest.raises(CloudSyncError):
        sync_control.preview_control(tmp_path, "disable", revision, interval)
    with pytest.raises(CloudSyncError):
        sync_control.preview_control(tmp_path, "enable", revision)


async def test_automatic_transport_error_is_recorded_not_silently_dropped(setup):
    _, _, engine, services, _ = setup
    stop = asyncio.Event()

    async def failure():
        stop.set()
        raise httpx.ReadTimeout("private-signed-url")

    engine.sync_once = AsyncMock(side_effect=failure)
    await engine.run(stop)
    assert engine._failures == 1
    assert "private-signed-url" not in engine._last_error
    assert engine._last_error
    await services.close()


async def test_queued_request_rejects_configuration_change(setup):
    root, _, engine, services, _ = setup
    revision = engine.inspect_scope(root)["revision"]
    async with project_execution(root):
        await services.control_sync(SyncControlBody(action="run_once", expected_revision=revision))
        await engine.update_scope(["figures"], revision)
    await services._manual_sync_task
    assert services._manual_job["state"] == "configuration_changed"
    engine._remote.assert_not_awaited()
    await services.close()


async def test_private_loopback_auth_is_never_in_url_or_result(monkeypatch):
    observed = []

    def handler(request):
        observed.append(request)
        assert "aero_access=test-local-capability" in request.headers["cookie"]
        return httpx.Response(200, json={"service_running": True})

    actual_client = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return actual_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sync_control.httpx, "AsyncClient", client)
    manager = SimpleNamespace(
        connection_info=lambda: {
            "base_url": "http://127.0.0.1:12345",
            "token": "test-local-capability",
        }
    )
    result = await sync_control._request(manager, "GET", "/api/v1/cloud/sync/control/status")
    assert "test-local-capability" not in str(result)
    assert not observed[0].url.query


async def test_control_client_uses_real_app_authentication_contract(setup, monkeypatch):
    from aero.server.app import WebRuntime, create_app

    root, _, _, services, _ = setup
    monkeypatch.setattr(WebRuntime, "_load_config", lambda _: AeroConfig.create_default())
    app, runtime = create_app(root, launch_token="test-real-app-capability")
    real_client = httpx.AsyncClient

    def client(**kwargs):
        return real_client(transport=httpx.ASGITransport(app=app), **kwargs)

    monkeypatch.setattr(sync_control.httpx, "AsyncClient", client)
    manager = SimpleNamespace(
        connection_info=lambda: {
            "base_url": "http://127.0.0.1:12345",
            "token": "test-real-app-capability",
        }
    )
    result = await sync_control._request(manager, "GET", "/api/v1/cloud/sync/control/status")
    assert result["service_running"] and result["configuration"]["project_id"] == "p"
    assert "test-real-app-capability" not in str(result)
    await runtime.close()
    await services.close()
