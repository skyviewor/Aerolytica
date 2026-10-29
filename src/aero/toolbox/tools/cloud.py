"""Official cloud tools shared by terminal, local Web and resident agents."""

from __future__ import annotations

import httpx

from aero.core.cloud_files import CloudFileError, CloudFiles
from aero.core.official_account import (
    OfficialAccountError,
    OfficialAccountSession,
    OfficialLoginRequiredError,
)
from aero.toolbox.registry import register_tool


async def _call(operation: str, **kwargs) -> dict:
    session = OfficialAccountSession()
    try:
        await session.access_token()
        result = await getattr(CloudFiles(session), operation)(**kwargs)
        return {
            "status": result.get("status", "success") if operation == "delete" else "success",
            "result": result,
        }
    except CloudFileError as exc:
        return {"status": "error", "http_status": exc.status_code, "message": str(exc)}
    except OfficialLoginRequiredError:
        return {
            "status": "error",
            "message": "请先使用 /login 登录官方账户，不要在对话里提供密码。",
        }
    except OfficialAccountError:
        return {
            "status": "error",
            "message": (
                "官方云空间操作未完成。请检查官方账户登录、云空间订阅、额度和目标权限；"
                "未登录时请使用 /login，不要在对话里提供密码。若上传完成确认中断，"
                "先查询云端文件，不要自动重复上传。"
            ),
        }
    except (ValueError, OSError) as exc:
        return {"status": "error", "message": str(exc)}
    except httpx.HTTPError:
        return {
            "status": "error",
            "message": ("云空间网络请求失败；请查询文件状态后再决定是否重试，不要声称传输成功。"),
        }
    finally:
        await session.close()


@register_tool(
    name="list_cloud_projects",
    description="查询 Aerolytica 官方云空间的项目和 ID。云端默认指官方云空间，不是第三方网盘。",
    parameters={"type": "object", "properties": {}},
)
async def list_cloud_projects() -> dict:
    return await _call("projects")


@register_tool(
    name="get_cloud_storage_quota",
    description=(
        "查询当前登录用户整个 Aerolytica 官方云空间的总容量、已用空间、上传预占、"
        "可上传剩余空间，以及文件/Agent 上下文占用、扩容、只读和超额状态。"
        "无需项目 ID，不从文件列表或积分余额推算。返回容量单位是字节，展示时注明单位；"
        "只有 enabled 且非 read_only 才可上传，available_bytes 为实际可上传余额。"
        "只读查询，不需要审批；结果为查询时快照，不保证后续上传一定有空间。"
    ),
    parameters={"type": "object", "properties": {}},
)
async def get_cloud_storage_quota() -> dict:
    return await _call("quota")


async def prepare_cloud_delete(project_id: str, files: list[dict]) -> dict:
    """Internal preflight; not a model tool and never authorizes deletion."""
    return await _call("prepare_delete", project_id=project_id, files=files)


@register_tool(
    name="delete_cloud_files",
    description=(
        "仅在用户明确要求删除官方云文件时使用。先 list_cloud_files 获取准确 ID、版本、"
        "完整相对路径和大小；一次提交 1–5 个文件。执行前系统核对真实清单并要求用户"
        "逐次审批，拒绝不删除；不得用通配符、目录、清空项目或会话永久授权扩大范围。"
        "只软删除指定云文件，不直接改动本地文件、目录或对话；若开启双向同步，"
        "后续同步可能将云端删除反映到本地。会话附件删除后可能无法在"
        "旧对话显示。版本改变则停止并重新查询审批。结果不确定或部分失败不得自动重试。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "files": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "expected_version": {"type": "integer", "minimum": 0},
                        "relative_path": {"type": "string", "maxLength": 255},
                        "size_bytes": {"type": "integer", "minimum": 0},
                    },
                    "required": ["file_id", "expected_version", "relative_path", "size_bytes"],
                },
            },
        },
        "required": ["project_id", "files"],
    },
    requires_confirmation=True,
)
async def delete_cloud_files(project_id: str, files: list[dict]) -> dict:
    return await _call("delete", project_id=project_id, files=files)


@register_tool(
    name="list_cloud_files",
    description=(
        "查询官方云空间指定项目的文件和目录，返回文件/目录 ID、路径、大小。"
        "项目 ID 从 list_cloud_projects 获取；has_more 时用 next_cursor 继续查询。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "cursor": {"type": "string"},
        },
        "required": ["project_id"],
    },
)
async def list_cloud_files(project_id: str, cursor: str | None = None) -> dict:
    return await _call("listing", project_id=project_id, cursor=cursor)


@register_tool(
    name="upload_cloud_file",
    description=(
        "用户要求上传/保存/同步指定文件到云端时，上传至 Aerolytica 官方云空间。"
        "先查询项目，用户未指定且存在多个项目时询问目标，不猜测。"
        "一次只上传明确授权的工作区普通文件，支持 data/ 下的总结；"
        "不启用目录持续同步、不递归上传、不上传凭据。云端同名冲突不会覆盖，"
        "请让用户决定新的文件名或目录。成功后才能声称已上传。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "本地工作区文件路径"},
            "project_id": {"type": "string", "description": "已确认的云项目 ID"},
            "directory_id": {"type": "string", "description": "云目录 ID；省略为项目根目录"},
        },
        "required": ["file_path", "project_id"],
    },
    requires_confirmation=True,
)
async def upload_cloud_file(
    file_path: str,
    project_id: str,
    directory_id: str | None = None,
) -> dict:
    return await _call(
        "upload", file_path=file_path, project_id=project_id, directory_id=directory_id
    )


@register_tool(
    name="download_cloud_file",
    description=(
        "将官方云空间文件下载到当前工作区。file_id 来自 list_cloud_files。"
        "本地父目录必须已存在；不覆盖已有文件，不下载到工作区外或凭据路径。"
        "这是一次性下载，不是持续同步。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_id": {"type": "string"},
            "file_path": {"type": "string", "description": "新的本地目标文件路径"},
        },
        "required": ["file_id", "file_path"],
    },
    requires_confirmation=True,
)
async def download_cloud_file(file_id: str, file_path: str) -> dict:
    return await _call("download", file_id=file_id, file_path=file_path)
