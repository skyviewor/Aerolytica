"""Local sync-scope inspection and approved configuration changes, never transfers."""

from __future__ import annotations

from aero.core.cloud_sync import CloudSyncEngine, CloudSyncError
from aero.core.official_account import OfficialAccountError, OfficialAccountSession
from aero.toolbox.paths import find_workspace_dir
from aero.toolbox.registry import register_tool


async def prepare_sync_control(
    action: str,
    expected_revision: str,
    interval_seconds: int | None = None,
) -> dict:
    from aero.core.sync_control import preview_control

    try:
        return {
            "status": "success",
            "result": preview_control(
                find_workspace_dir(),
                action,
                expected_revision,
                interval_seconds,
            ),
        }
    except (CloudSyncError, ValueError, OSError, KeyError, TypeError) as exc:
        return {"status": "error", "message": str(exc)}


@register_tool(
    name="get_cloud_sync_status",
    description=(
        "查询当前工作区的云文件同步配置和实际后台运行状态：绑定、范围、开关、间隔、"
        "最近同步时间、冲突/预占及手动任务状态。只读，不启动后台服务，无需审批。"
        "automatic_enabled 只代表配置，automatic_task_running 才代表自动任务存活。"
        "不要把文件同步和聊天上下文同步混淆。"
    ),
    parameters={"type": "object", "properties": {}},
)
async def get_cloud_sync_status() -> dict:
    from aero.core.sync_control import control_status

    try:
        return {"status": "success", "result": await control_status(find_workspace_dir())}
    except Exception:
        return {"status": "error", "message": "未能确认同步运行状态，不能据此声称同步正常。"}


async def _apply_sync_control(action: str, expected_revision: str, interval_seconds=None) -> dict:
    from aero.core.sync_control import apply_control

    try:
        result = await apply_control(
            find_workspace_dir(), action, expected_revision, interval_seconds
        )
        return {"status": result["status"], "result": result}
    except Exception:
        return {
            "status": "error",
            "message": ("同步控制未确认成功，请重新查询状态。不要自动重试或宣称已同步完成。"),
        }


@register_tool(
    name="set_cloud_sync_enabled",
    description=(
        "用户要求开启、关闭自动文件同步或调整间隔时使用。先 get_cloud_sync_status 获取"
        "configuration.revision，传 enabled 和 expected_revision，可选 interval_seconds"
        "（30–900 秒）。每次须审批。开启会启动现有本机常驻服务，关闭聊天后仍运行，"
        "但不安装开机自启；关闭会取消本服务的自动/手动同步任务，不停止聊天 Agent。"
        "不开新云项目、不改同步范围；未绑定时不能开启。切换开关可能中断在途传输，"
        "保留待处理状态。只报告已确认的开关和后台状态，不把开启当成已同步完成。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "expected_revision": {"type": "string"},
            "interval_seconds": {"type": "integer", "minimum": 30, "maximum": 900},
        },
        "required": ["enabled", "expected_revision"],
    },
    requires_confirmation=True,
)
async def set_cloud_sync_enabled(
    enabled: bool,
    expected_revision: str,
    interval_seconds: int | None = None,
    **_preview,
) -> dict:
    if type(enabled) is not bool:
        return {"status": "error", "message": "enabled 必须为布尔值。"}
    return await _apply_sync_control(
        "enable" if enabled else "disable", expected_revision, interval_seconds
    )


@register_tool(
    name="sync_cloud_now",
    description=(
        "用户要求立刻执行一次云文件双向同步时使用。先读取同步状态的 configuration.revision，"
        "传 expected_revision，每次须审批。只处理当前已批准范围，可能上传/下载或传播删除。"
        "使用现有本机常驻服务，不改变自动开关或间隔；关闭自动同步时也可单次执行。"
        "工作区忙时排队，返回 queued 不代表完成：应先告知用户已排队并结束本轮，不要在"
        "同一轮持续轮询等待（本轮任务持有工作区锁）。之后可查 get_cloud_sync_status。"
        "部分冲突不自动覆盖，不自动重试。"
    ),
    parameters={
        "type": "object",
        "properties": {"expected_revision": {"type": "string"}},
        "required": ["expected_revision"],
    },
    requires_confirmation=True,
)
async def sync_cloud_now(expected_revision: str, **_preview) -> dict:
    return await _apply_sync_control("run_once", expected_revision)


async def prepare_sync_scope(includes: list[str], expected_revision: str) -> dict:
    try:
        result = CloudSyncEngine.preview_scope_at(find_workspace_dir(), includes, expected_revision)
        return {"status": "success", "result": result}
    except (CloudSyncError, ValueError, OSError, KeyError, TypeError):
        return {
            "status": "error",
            "message": (
                "无法核对同步范围。请重新读取当前配置；只支持 1–20 个工作区一级普通目录，"
                "不可包含硬性排除、隐藏路径、凭据或链接。"
            ),
        }


@register_tool(
    name="get_local_sync_directories",
    description=(
        "读取当前本地工作区的云同步目录、默认目录、硬性排除规则、绑定与暂停状态，"
        "以及修改时必需的 revision。不访问网络、不创建配置文件，不需要审批。"
        "配置范围不等于同步服务正在运行；未绑定时不会同步。"
    ),
    parameters={"type": "object", "properties": {}},
)
async def get_local_sync_directories() -> dict:
    try:
        return {"status": "success", "result": CloudSyncEngine.inspect_scope(find_workspace_dir())}
    except (CloudSyncError, ValueError, OSError, KeyError, TypeError):
        return {"status": "error", "message": "无法安全读取当前工作区的同步配置。"}


@register_tool(
    name="set_local_sync_directories",
    description=(
        "仅在用户要求修改云同步范围时使用。先读取 get_local_sync_directories，提交新的完整"
        "includes 列表及 expected_revision；这是替换范围，不是追加。一次支持 1–20 个"
        "工作区一级目录（如 papers、figures 或 figures/**），不支持嵌套范围。"
        "系统展示真实旧范围、新范围和增减目录，必须逐次获得批准，不使用会话始终允许。"
        "硬性排除和凭据保护不可放开。不改账户/云项目绑定，不启用、恢复或立即运行同步；"
        "未绑定时仅保存后续默认选择。新增目录在后续同步时可能上传/下载并传播删除；"
        "移除目录仅退出同步范围，不删除两端现存文件。配置变化或存在待完成上传时停止。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "includes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 128},
            },
            "expected_revision": {"type": "string", "description": "最近读取返回的 revision"},
        },
        "required": ["includes", "expected_revision"],
    },
    requires_confirmation=True,
)
async def set_local_sync_directories(
    includes: list[str],
    expected_revision: str,
    **_preview,
) -> dict:
    session = OfficialAccountSession()
    try:
        CloudSyncEngine.preview_scope_at(find_workspace_dir(), includes, expected_revision)
        current = CloudSyncEngine.inspect_scope(find_workspace_dir())
        if current["bound"]:
            await session.access_token()
        result = await CloudSyncEngine(find_workspace_dir(), session).update_scope(
            includes,
            expected_revision,
        )
        return {
            "status": "success",
            "result": result,
            "message": "同步范围已保存；未启用或立即运行同步，未删除两端文件。",
        }
    except (CloudSyncError, ValueError, OSError, KeyError, TypeError):
        return {
            "status": "error",
            "message": (
                "同步范围未能更新：请重新读取并审批，检查配置是否变化、账号绑定是否匹配、"
                "同步是否正在占用锁或仍有待完成上传。安全限制不会被放开。"
            ),
        }
    except OfficialAccountError:
        return {"status": "error", "message": "请先 /login 登录原同步绑定的官方账户。"}
    finally:
        await session.close()
