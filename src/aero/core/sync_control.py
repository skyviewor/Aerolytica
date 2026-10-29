"""Approved file-sync controls over the existing authenticated loopback service."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from aero.core.cloud_sync import CloudSyncEngine, CloudSyncError
from aero.core.daemon import DaemonManager
from aero.core.official_account import OfficialAccountSession


def preview_control(
    root: Path, action: str, expected_revision: str, interval_seconds: int | None = None
) -> dict:
    scope = CloudSyncEngine.inspect_scope(root)
    if action not in {"enable", "disable", "run_once"} or scope["revision"] != expected_revision:
        raise CloudSyncError("同步配置已变化，请重新查看并审批。")
    if interval_seconds is not None and (
        type(interval_seconds) is not int or not 30 <= interval_seconds <= 900
    ):
        raise CloudSyncError("同步间隔应为 30–900 秒的整数。")
    if action != "disable" and not scope["bound"]:
        raise CloudSyncError("尚未绑定云项目；请先绑定，不能替用户自动选择目标。")
    result = {
        "expected_revision": expected_revision,
        "action": action,
        "interval_seconds": interval_seconds,
        "workspace": scope["workspace"],
        "project_id": scope["project_id"],
        "includes": scope["includes"],
        "previous_paused": scope["paused"],
        "previous_interval_seconds": scope["interval_seconds"],
    }
    if action != "run_once":
        result["enabled"] = action == "enable"
    return result


async def _request(manager, method: str, path: str, body: dict | None = None) -> dict:
    connection = await asyncio.to_thread(manager.connection_info)
    async with httpx.AsyncClient(
        base_url=connection["base_url"],
        trust_env=False,
        timeout=20,
        cookies={"aero_access": connection["token"]},
    ) as http:
        response = await http.request(method, path, json=body)
        if response.is_error:
            raise CloudSyncError("本机同步控制未完成，请重新查询状态后再决定是否重试。")
        return response.json()


async def control_status(root: Path) -> dict:
    scope = CloudSyncEngine.inspect_scope(root)
    manager = DaemonManager(root)
    status = await asyncio.to_thread(manager.status)
    if not status["running"]:
        return {
            "configuration": scope,
            "service_running": False,
            "automatic_task_running": False,
            "transfer_in_progress": False,
            "manual_job": {},
            "note": "后台服务未运行；配置开关不代表正在同步。",
        }
    return await _request(manager, "GET", "/api/v1/cloud/sync/control/status")


async def apply_control(
    root: Path, action: str, expected_revision: str, interval_seconds: int | None = None
) -> dict:
    preview_control(root, action, expected_revision, interval_seconds)
    manager = DaemonManager(root)
    running = (await asyncio.to_thread(manager.status))["running"]
    if action == "disable" and not running:
        session = OfficialAccountSession()
        try:
            scope = await CloudSyncEngine(root, session).configure_sync(
                False,
                expected_revision,
                interval_seconds,
            )
        finally:
            await session.close()
        return {
            "status": "success",
            "configuration": scope,
            "service_running": False,
            "automatic_task_running": False,
            "transfer_in_progress": False,
            "manual_job": {},
        }
    if not running:
        # Explicitly approved enable/one-shot request; never opens a browser or
        # installs an OS autostart job. The existing daemon survives TUI exit.
        await asyncio.to_thread(manager.start)
    return await _request(
        manager,
        "POST",
        "/api/v1/cloud/sync/control",
        {
            "action": action,
            "expected_revision": expected_revision,
            "interval_seconds": interval_seconds,
        },
    )
