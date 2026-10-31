"""Never expose activation/restart or arbitrary download addresses to models."""

from __future__ import annotations

import asyncio

from aero.toolbox.registry import register_tool


@register_tool(
    name="check_client_update",
    description="检查官方签名发行版，不下载安装。只讨论版本时先检查，未经同意不准备更新。",
    parameters={"type": "object", "properties": {}},
)
async def check_client_update() -> dict:
    from aero.cli.update import check_update
    from aero.distribution import DistributionError
    from aero.version import get_version

    try:
        release = await asyncio.to_thread(check_update)
        return {
            "status": "success",
            "running_version": get_version(),
            "available_version": release["manifest"]["version"] if release else None,
        }
    except DistributionError as exc:
        return {"status": "error", "message": str(exc)}


@register_tool(
    name="prepare_client_update",
    description=(
        "后台准备官方客户端更新。用户明确要求升级即授权准备；仅讨论版本时先询问。"
        "不切换当前版本、不退出对话、不重启 Agent，准备好后让用户手动 /restart"
        "或点击重启并应用。不可执行任意升级 Shell/下载地址。重复调用复用任务，"
        "不要持续轮询。reinstall 仅用于用户明确要求重装同版本测试修订。"
    ),
    parameters={
        "type": "object",
        "properties": {"reinstall": {"type": "boolean"}},
        "additionalProperties": False,
    },
)
async def prepare_client_update(reinstall: bool = False) -> dict:
    from aero.cli.pending_update import prepare_update
    from aero.distribution import DistributionError

    try:
        return await asyncio.to_thread(prepare_update, reinstall=reinstall)
    except (DistributionError, OSError, ValueError) as exc:
        return {
            "status": "error",
            "message": str(exc)
            if isinstance(exc, DistributionError)
            else "未能准备更新，当前版本未切换。",
        }


@register_tool(
    name="get_client_update_status",
    description="读取本机更新准备状态，不请求云端，不应用或重启。不要高频轮询。",
    parameters={"type": "object", "properties": {}},
)
async def get_client_update_status() -> dict:
    from aero.cli.pending_update import update_status
    from aero.distribution import DistributionError

    try:
        return await asyncio.to_thread(update_status)
    except (DistributionError, OSError, ValueError):
        return {"status": "error", "message": "待更新状态无法验证，请运行 aero update --check。"}
