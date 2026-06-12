"""Download record query, retry, and cleanup tools."""

import os
from pathlib import Path

from aero.toolbox.config_access import find_config
from aero.toolbox.download_progress import download_progress_reporter
from aero.toolbox.paths import find_project_dir, short_path
from aero.toolbox.registry import register_tool


@register_tool(
    name="list_downloads",
    description=(
        "列出本地 ERA5 CDS 下载记录。可查看所有下载、最近下载、或未完成的下载。"
        "用户问「之前下载怎么样了」「有没有下载失败的」时使用。\n\n"
        "status 参数:\n"
        "  不填 → 最近 20 条记录\n"
        "  'incomplete' → 所有未完成的（下载中/失败/排队/错误）\n"
        "  'completed_with_file' → 已完成的下载\n"
        "  'download_failed' → 下载失败的记录。CDS 可用 retry_download 续传；"
        "GCS/AWS 应按原参数重新提交下载"
    ),
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": (
                    "过滤状态: incomplete / completed_with_file / download_failed / error。"
                    "不填返回全部"
                ),
            },
            "limit": {
                "type": "integer",
                "description": "返回条数，默认 20",
            },
        },
    },
)
async def list_downloads(status: str | None = None, limit: int = 20) -> dict:
    """List CDS download records from local SQLite store."""
    from aero.data.download_store import CDSDownloadStore

    project_dir = find_project_dir()
    store = CDSDownloadStore(project_dir / "aero_downloads.db")

    if status == "incomplete":
        records = store.list_incomplete()
    elif status:
        records = store.list_by_status(status, limit=limit)
    else:
        records = store.list_all(limit=limit)

    stats = store.get_stats()

    downloads = []
    for r in records:
        downloads.append(
            {
                "id": r["id"],
                "source": r.get("source"),
                "request_id": r.get("request_id"),
                "dataset_id": r.get("dataset_id"),
                "variables": r.get("variables", []),
                "year": r.get("year"),
                "month": r.get("month"),
                "day": r.get("day"),
                "pressure_level": r.get("pressure_level"),
                "area": r.get("area"),
                "data_format": r.get("data_format"),
                "status": r["status"],
                "file_path": short_path(str(r.get("file_path", ""))),
                "file_size": r.get("file_size"),
                "total_bytes": r.get("total_bytes"),
                "downloaded_bytes": r.get("downloaded_bytes", 0),
                "error_msg": r.get("error_msg"),
                "notes": r.get("notes"),
                "submitted_at": r.get("submitted_at"),
            }
        )

    return {
        "returned": len(downloads),
        "stats": stats,
        "downloads": downloads,
    }


@register_tool(
    name="query_download",
    description=("查询单个 ERA5 下载任务的详细状态和进度。用户指定任务 ID 或 request_id 时使用。"),
    parameters={
        "type": "object",
        "properties": {
            "download_id": {
                "type": "integer",
                "description": "本地下载记录 ID。跟 request_id 至少传一个",
            },
            "request_id": {
                "type": "string",
                "description": "CDS 请求 ID。跟 download_id 至少传一个",
            },
        },
    },
)
async def query_download(download_id: int | None = None, request_id: str | None = None) -> dict:
    """Query a single CDS download record."""
    from aero.data.download_store import CDSDownloadStore

    if not download_id and not request_id:
        return {"status": "error", "message": "download_id 和 request_id 至少传一个"}

    project_dir = find_project_dir()
    store = CDSDownloadStore(project_dir / "aero_downloads.db")

    record = store.get(request_id=request_id, row_id=download_id)
    if not record:
        return {"status": "not_found", "message": "未找到该下载记录"}

    total = record.get("total_bytes") or 0
    downloaded = record.get("downloaded_bytes") or 0
    progress = f"{downloaded / total * 100:.1f}%" if total > 0 else "未知"

    return {
        "id": record["id"],
        "source": record.get("source"),
        "request_id": record.get("request_id"),
        "dataset_id": record.get("dataset_id"),
        "variables": record.get("variables", []),
        "year": record.get("year"),
        "month": record.get("month"),
        "day": record.get("day"),
        "pressure_level": record.get("pressure_level"),
        "area": record.get("area"),
        "data_format": record.get("data_format"),
        "status": record["status"],
        "file_path": short_path(str(record.get("file_path", ""))),
        "file_size": record.get("file_size"),
        "download_url": record.get("download_url"),
        "total_bytes": total,
        "downloaded_bytes": downloaded,
        "progress": progress,
        "error_msg": record.get("error_msg"),
        "notes": record.get("notes"),
        "submitted_at": record.get("submitted_at"),
        "completed_at": record.get("completed_at"),
    }


@register_tool(
    name="retry_download",
    description=(
        "重试一个已提交但下载失败的 CDS 任务。支持断点续传——如果本地已有部分数据且 CDS URL 仍有效，"
        "将从断点继续下载而不重新提交任务。仅适用于 CDS 下载失败记录。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "download_id": {
                "type": "integer",
                "description": "要重试的本地下载记录 ID",
            },
        },
        "required": ["download_id"],
    },
)
async def retry_download(download_id: int) -> dict:
    """Retry a failed CDS download — supports HTTP Range resume."""
    from aero.adapters.cds_adapter import CDSAdapter
    from aero.data.download_store import CDSDownloadStore

    config = find_config()
    cds_cfg = config.credentials.cds
    project_dir = find_project_dir()
    store = CDSDownloadStore(project_dir / "aero_downloads.db")

    record = store.get(row_id=download_id)
    if not record:
        return {"status": "not_found", "message": "未找到该下载记录"}

    download_url = record.get("download_url")
    file_path = Path(record.get("file_path", ""))
    status = record.get("status")

    if not cds_cfg.key:
        return {
            "status": "error",
            "message": "CDS API key 未配置，无法重试。",
        }

    adapter = CDSAdapter(cds_url=cds_cfg.url, cds_key=cds_cfg.key)

    if download_url and file_path:
        resume_from = os.path.getsize(str(file_path)) if file_path.exists() else 0
    else:
        resume_from = 0

    if download_url and status in ("download_failed", "error"):
        try:
            from aero.agent.progress import emit_progress

            emit_progress(f"正在继续下载文件：{short_path(file_path)}")

            file_size = await adapter.fetch(
                download_url=download_url,
                dest_path=file_path,
                resume_from=resume_from,
                on_progress=download_progress_reporter(),
                total_bytes=record.get("total_bytes") or 0,
            )
            store.update_by_id(
                download_id, status="completed_with_file", file_size=file_size, error_msg=""
            )
            return {
                "status": "success",
                "message": f"已继续下载完成：{short_path(file_path)}",
                "download_id": download_id,
                "file_path": short_path(file_path),
            }
        except Exception as e:
            store.update_by_id(download_id, status="download_failed", error_msg=str(e))
            return {
                "status": "error",
                "message": f"续传失败：{e}。URL 可能已过期，建议按原始参数重新提交下载。",
                "download_id": download_id,
            }

    if status == "error":
        return {
            "status": "error",
            "message": (
                "CDS 任务提交失败（error 状态），无法从 URL 续传。请按原始参数重新提交下载。"
            ),
            "download_id": download_id,
        }

    return {
        "status": "error",
        "message": (
            f"不支持重试当前状态 ({status})。只有 download_failed / error 状态的记录才能重试。"
        ),
        "download_id": download_id,
    }


@register_tool(
    name="cleanup_downloads",
    description=(
        "清理本地下载记录数据库。不会删除实际数据文件。"
        "用户说「清理已完成的下载记录」「清除失败记录」时使用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "清理目标: 'all_completed' 清除所有已完成的记录, "
                    "'all_failed' 清除所有失败的记录, "
                    "或逗号分隔的 ID 列表如 '1,3,5'"
                ),
            },
        },
        "required": ["target"],
    },
)
async def cleanup_downloads(target: str) -> dict:
    """Clean up download records from local SQLite store."""
    from aero.data.download_store import CDSDownloadStore

    project_dir = find_project_dir()
    store = CDSDownloadStore(project_dir / "aero_downloads.db")

    if target == "all_completed":
        deleted = store.delete_by_status("completed_with_file")
        return {
            "status": "success",
            "message": f"已清理 {deleted} 条已完成的下载记录。实际数据文件未删除。",
            "deleted": deleted,
        }

    if target == "all_failed":
        deleted = store.delete_by_status("download_failed")
        deleted += store.delete_by_status("error")
        return {
            "status": "success",
            "message": f"已清理 {deleted} 条失败的下载记录。实际数据文件未删除。",
            "deleted": deleted,
        }

    try:
        ids = [int(x.strip()) for x in target.split(",") if x.strip()]
        deleted = store.delete_by_ids(ids)
        return {
            "status": "success",
            "message": f"已清理 {deleted} 条下载记录。实际数据文件未删除。",
            "deleted": deleted,
        }
    except ValueError:
        return {
            "status": "error",
            "message": (
                "target 格式错误，请使用 'all_completed' / 'all_failed' 或 ID 列表如 '1,3,5'"
            ),
        }
