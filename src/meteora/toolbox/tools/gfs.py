"""GFS forecast schedule, availability, inventory, and download tools."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from meteora.data.download_store import CDSDownloadStore
from meteora.toolbox.download_progress import download_progress_reporter
from meteora.toolbox.paths import find_project_dir, short_path
from meteora.toolbox.registry import register_tool


@register_tool(
    name="get_gfs_forecast_schedule",
    description=(
        "根据 GFS 官方预报时效间隔，把用户请求的起报后时间窗口解析成应下载的 forecast_hours。"
        "GFS 预报时效间隔取决于产品；0.25° 产品前段通常逐小时，"
        "0.5°/1.0° 产品通常是 f000 后每 3 小时。"
        "历史 0.25° 产品还需要按起报日期区分，2021-06-12 之前的后段可能退化到 12 小时。"
        "用户说下载未来 N 小时、某段预报时效、连续小时数据时，应先调用此工具，不要默认每 3 小时。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "start_hour": {
                "type": "integer",
                "description": "起始预报时效，默认 0。例如从起报时刻开始就是 0。",
            },
            "end_hour": {
                "type": "integer",
                "description": "结束预报时效，包含端点。例如未来 12 小时传 12。",
            },
            "duration_hours": {
                "type": "integer",
                "description": "持续小时数；如果给了 end_hour，可不传。未来 12 小时通常传 12。",
            },
            "product": {
                "type": "string",
                "description": "GFS 产品名，默认 pgrb2.0p25。",
            },
            "date": {
                "type": "string",
                "description": (
                    "GFS 起报日期 YYYYMMDD 或 YYYY-MM-DD。历史数据必须传日期以使用正确时效规则。"
                ),
            },
            "cycle": {
                "type": "string",
                "description": (
                    "GFS 起报时次，可传 00、06、12、18 或 00z。"
                    "当前用于回传上下文并兼容模型调用；具体可用性由下载/可用性检查确认。"
                ),
            },
        },
    },
)
async def get_gfs_forecast_schedule(
    start_hour: int = 0,
    end_hour: int | None = None,
    duration_hours: int | None = None,
    product: str = "pgrb2.0p25",
    date: str | None = None,
    cycle: str | None = None,
) -> dict:
    """Resolve requested GFS forecast window to actual forecast-hour outputs."""
    from meteora.data.gfs_availability import gfs_forecast_hours_for_range
    from meteora.adapters.gfs_adapter import normalize_cycle

    try:
        normalized_cycle = normalize_cycle(cycle) if cycle is not None else None
        schedule = gfs_forecast_hours_for_range(
            start_hour=start_hour,
            end_hour=end_hour,
            duration_hours=duration_hours,
            product=product,
            date=date,
        )
    except Exception as e:
        return {"status": "error", "message": f"GFS 预报时效解析失败：{e}"}

    if normalized_cycle is not None:
        schedule["cycle"] = normalized_cycle

    hours = schedule["forecast_hours"]
    if not hours:
        return {
            "status": "error",
            "message": "这个时间窗口内没有匹配的 GFS 预报输出时效。",
            **schedule,
        }
    return {
        "status": "success",
        "message": (
            f"GFS 预报时效已解析：f{hours[0]:03d}-f{hours[-1]:03d}，共 {len(hours)} 个文件。"
        ),
        "download_hint": (
            "把 forecast_hours 原样传给 download_gfs；不要跨产品、跨日期套用固定间隔。"
            "下载或可用性检查会继续确认具体远端文件是否存在。"
        ),
        **schedule,
    }


@register_tool(
    name="download_gfs",
    description=(
        "从 NOAA/NCEP GFS 下载预报场。默认优先使用 NOMADS 官网；官网没有较早时次时，"
        "自动尝试 AWS OpenData 历史归档。"
        "支持根据官方 .idx 文件按变量和层级分块下载，只保存命中的 GRIB2 message。"
        "v1 不支持经纬度裁剪；variables 使用 GRIB short name，如 TMP、HGT、UGRD、VGRD、APCP、RH。"
        "如果用户给的是时间窗口或持续小时数，先用 get_gfs_forecast_schedule 解析 forecast_hours，"
        "不要默认每 3 小时。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "起报日期，YYYYMMDD 或 YYYY-MM-DD，如 20260604",
            },
            "cycle": {
                "type": "string",
                "enum": ["00", "06", "12", "18"],
                "description": "起报时次 UTC，只支持 00、06、12、18",
            },
            "forecast_hours": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "预报时效列表，如 [0, 6, 12]；每个时效输出一个 .grib2 文件",
            },
            "variables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "GRIB short name 列表，如 ['TMP', 'HGT']",
            },
            "product": {
                "type": "string",
                "description": (
                    "GFS 产品名，默认 pgrb2.0p25。可传 pgrb2b.0p25、pgrb2.0p50、"
                    "pgrb2.1p00 等官方 NCO 产品清单中的文件产品"
                ),
            },
            "levels": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "可选层级精确匹配 .idx 中的 level 文本，如 ['500 mb', '2 m above ground']。"
                    "不填则下载指定变量在该文件中的全部层级"
                ),
            },
            "source": {
                "type": "string",
                "enum": ["auto", "nomads", "aws"],
                "description": "数据来源，默认 auto：优先官网，官网没有时自动回退 AWS 历史归档",
            },
        },
        "required": ["date", "cycle", "forecast_hours", "variables"],
    },
)
async def download_gfs(
    date: str,
    cycle: str,
    forecast_hours: list[int],
    variables: list[str],
    product: str = "pgrb2.0p25",
    levels: list[str] | None = None,
    source: str = "auto",
) -> dict:
    """Download selected GFS GRIB2 messages via .idx byte ranges."""
    from meteora.adapters.gfs_adapter import (
        GFSAdapter,
        build_request_id,
        dataset_id_for_product,
        normalize_forecast_hours,
    )
    from meteora.agent.progress import emit_progress
    from meteora.data.gfs_availability import resolve_gfs_source

    project_dir = find_project_dir()
    store = CDSDownloadStore(project_dir / "meteora_downloads.db")

    try:
        normalized_hours = normalize_forecast_hours(forecast_hours)
        results = []
        skipped = []
        for fhour in normalized_hours:
            decision = await resolve_gfs_source(
                date=date,
                cycle=cycle,
                product=product,
                forecast_hour=fhour,
                source=source,
            )
            if not decision.available or decision.selected is None:
                return {
                    "status": "error",
                    "message": f"GFS 下载失败：{decision.reason}",
                    "date": decision.date,
                    "cycle": decision.cycle,
                    "product": decision.product,
                    "forecast_hour": decision.forecast_hour,
                    "requested_source": decision.requested_source,
                    "availability": _gfs_decision_to_dict(decision),
                }
            if decision.selected_source == "aws" and decision.requested_source == "auto":
                emit_progress("官网没有这个时次，正在尝试从 AWS 历史归档获取")
            adapter = GFSAdapter(base_url=decision.selected.base_url)
            try:
                result = await adapter.download_one(
                    date=date,
                    cycle=cycle,
                    forecast_hour=fhour,
                    variables=variables,
                    product=product,
                    levels=levels,
                    on_progress=download_progress_reporter(),
                )
            except RuntimeError as e:
                if "GFS .idx 中没有找到匹配字段" not in str(e):
                    raise
                skipped.append(
                    {
                        "forecast_hour": fhour,
                        "reason": str(e),
                        "source_checked": decision.selected_source,
                        "idx_url": decision.selected.idx_url,
                    }
                )
                emit_progress(f"GFS f{fhour:03d} 未找到匹配字段，已跳过")
                continue
            results.append(
                (
                    replace(result, source=decision.selected_source or "unknown"),
                    decision,
                )
            )
    except BaseException as e:
        if isinstance(e, asyncio.CancelledError):
            raise
        return {"status": "error", "message": f"GFS 下载失败：{e}"}

    if not results:
        return {
            "status": "error",
            "message": "GFS 下载失败：所有请求时效的 .idx 中都没有找到匹配字段。",
            "skipped_forecast_hours": skipped,
            "variables": [v.upper() for v in variables],
            "levels": levels,
        }

    files = []
    total_bytes = 0
    all_missing = []
    sources_used = []
    for result, decision in results:
        request_id = build_request_id(
            date=date,
            cycle=cycle,
            forecast_hour=result.forecast_hour,
            variables=variables,
            levels=levels,
            product=product,
        )
        dataset_id = dataset_id_for_product(product)
        selected = [
            {
                "variable": entry.variable,
                "level": entry.level,
                "forecast": entry.forecast,
                "range": entry.range_header,
            }
            for entry in result.selected_entries
        ]
        notes = {
            "product": product,
            "requested_source": source,
            "data_source": result.source,
            "availability": _gfs_decision_to_dict(decision),
            "idx_url": result.idx_url,
            "grib_url": result.grib_url,
            "selected_messages": len(result.selected_entries),
            "missing": result.missing,
            "range_total_bytes": result.downloaded_bytes,
        }
        row_id = store.insert(
            source="gfs",
            request_id=request_id,
            dataset_id=dataset_id,
            variables=[v.upper() for v in variables],
            file_path=str(result.file_path),
            file_size=result.downloaded_bytes,
            download_url=result.grib_url,
            status="completed_with_file",
            total_bytes=result.downloaded_bytes,
            downloaded_bytes=result.downloaded_bytes,
            data_format="grib2",
            notes=json.dumps(notes, ensure_ascii=False),
        )
        total_bytes += result.downloaded_bytes
        all_missing.extend(result.missing)
        sources_used.append(result.source)
        files.append(
            {
                "download_id": row_id,
                "request_id": request_id,
                "forecast_hour": result.forecast_hour,
                "source_used": result.source,
                "file_path": short_path(result.file_path),
                "file_size": result.downloaded_bytes,
                "idx_url": result.idx_url,
                "selected_messages": len(result.selected_entries),
                "selected": selected,
                "missing": result.missing,
            }
        )

    return {
        "status": "success",
        "source": "gfs",
        "dataset_id": dataset_id_for_product(product),
        "date": date,
        "cycle": cycle,
        "product": product,
        "requested_source": source,
        "sources_used": sorted(set(sources_used)),
        "variables": [v.upper() for v in variables],
        "levels": levels,
        "files": files,
        "total_files": len(files),
        "total_bytes": total_bytes,
        "missing": all_missing,
        "skipped_forecast_hours": skipped,
        "note": "GFS 使用官方 .idx + HTTP Range 分块下载，不做经纬度裁剪。",
        "references": [
            "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/",
            "https://registry.opendata.aws/noaa-gfs-bdp-pds/",
            "https://noaa-gfs-bdp-pds.s3.amazonaws.com",
        ],
    }


@register_tool(
    name="check_gfs_availability",
    description=(
        "检查 GFS 官网和 AWS OpenData 当前支持哪些日期/时次。"
        "不传 date 时返回两个来源的可用范围；传 date/cycle 时检查目标产品和时效是否可下载。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "可选，起报日期，YYYYMMDD 或 YYYY-MM-DD",
            },
            "cycle": {
                "type": "string",
                "enum": ["00", "06", "12", "18"],
                "description": "可选，起报时次 UTC；传 date 时建议同时传 cycle",
            },
            "product": {
                "type": "string",
                "description": "GFS 产品名，默认 pgrb2.0p25",
            },
            "forecast_hour": {
                "type": "integer",
                "description": "预报时效，默认 0",
            },
            "refresh": {
                "type": "boolean",
                "description": "是否绕过本地缓存重新查询目录",
            },
        },
    },
)
async def check_gfs_availability(
    date: str | None = None,
    cycle: str | None = None,
    product: str = "pgrb2.0p25",
    forecast_hour: int = 0,
    refresh: bool = False,
) -> dict:
    from meteora.data.gfs_availability import (
        AWS_REGISTRY_URL,
        cache_path,
        get_gfs_availability,
        resolve_gfs_source,
    )

    try:
        if date:
            if not cycle:
                return {
                    "status": "error",
                    "message": "检查指定日期时需要同时提供起报时次，例如 00、06、12 或 18。",
                }
            decision = await resolve_gfs_source(
                date=date,
                cycle=cycle,
                product=product,
                forecast_hour=forecast_hour,
                source="auto",
            )
            return {
                "status": "success",
                "mode": "target",
                "available": decision.available,
                "recommended_source": decision.selected_source,
                "reason": decision.reason,
                "target": {
                    "date": decision.date,
                    "cycle": decision.cycle,
                    "product": decision.product,
                    "forecast_hour": decision.forecast_hour,
                },
                "availability": _gfs_decision_to_dict(decision),
                "references": [
                    "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/",
                    AWS_REGISTRY_URL,
                    "https://noaa-gfs-bdp-pds.s3.amazonaws.com",
                ],
            }

        summary = await get_gfs_availability(refresh=refresh)
        return {
            "status": "success",
            "mode": "range",
            "nomads": summary["nomads"],
            "aws": summary["aws"],
            "cache_path": str(cache_path()),
            "references": [
                "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/",
                AWS_REGISTRY_URL,
                "https://noaa-gfs-bdp-pds.s3.amazonaws.com",
            ],
        }
    except BaseException as e:
        if isinstance(e, asyncio.CancelledError):
            raise
        return {"status": "error", "message": f"GFS 可用性检查失败：{e}"}


@register_tool(
    name="inspect_gfs_inventory",
    description=(
        "查看单个 GFS GRIB2 文件的官方 .idx 库存，返回指定变量在该文件中的 level、forecast 文本、"
        "message 数和字节范围信息。用于下载前确认 APCP/TMP/RH 等变量的实际层级；"
        "不要用 run_shell/curl/grep 直接查看 NOMADS .idx。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "起报日期，YYYYMMDD 或 YYYY-MM-DD",
            },
            "cycle": {
                "type": "string",
                "enum": ["00", "06", "12", "18"],
                "description": "起报时次 UTC，只支持 00、06、12、18",
            },
            "forecast_hour": {
                "type": "integer",
                "description": "预报时效，如 0、3、12",
            },
            "variables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，GRIB short name 列表，如 ['APCP']；不填返回全部变量汇总",
            },
            "product": {
                "type": "string",
                "description": "GFS 产品名，默认 pgrb2.0p25",
            },
            "source": {
                "type": "string",
                "enum": ["auto", "nomads", "aws"],
                "description": "数据来源，默认 auto：优先官网，官网没有时自动回退 AWS 历史归档",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回多少条库存汇总，默认 100",
            },
        },
        "required": ["date", "cycle", "forecast_hour"],
    },
)
async def inspect_gfs_inventory(
    date: str,
    cycle: str,
    forecast_hour: int,
    variables: list[str] | None = None,
    product: str = "pgrb2.0p25",
    source: str = "auto",
    limit: int = 100,
) -> dict:
    from meteora.adapters.gfs_adapter import GFSAdapter, parse_gfs_idx, summarize_gfs_inventory
    from meteora.data.gfs_availability import resolve_gfs_source

    try:
        decision = await resolve_gfs_source(
            date=date,
            cycle=cycle,
            product=product,
            forecast_hour=forecast_hour,
            source=source,
        )
        if not decision.available or decision.selected is None:
            return {
                "status": "unavailable",
                "message": f"GFS 目标文件暂不可用：{decision.reason}",
                "availability": _gfs_decision_to_dict(decision),
            }
        adapter = GFSAdapter(base_url=decision.selected.base_url)
        grib_url = adapter.build_grib_url(
            decision.date,
            decision.cycle,
            decision.forecast_hour,
            decision.product,
        )
        idx_url = f"{grib_url}.idx"
        idx_text = await asyncio.to_thread(adapter._fetch_text, idx_url)
        entries = parse_gfs_idx(idx_text)
        inventory = summarize_gfs_inventory(entries, variables)
        limit = max(1, min(int(limit), 500))
        return {
            "status": "success",
            "date": decision.date,
            "cycle": decision.cycle,
            "product": decision.product,
            "forecast_hour": decision.forecast_hour,
            "source_used": decision.selected_source,
            "idx_url": idx_url,
            "variables": [v.upper() for v in variables] if variables else None,
            "total_messages": len(entries),
            "matched": len(inventory),
            "inventory": inventory[:limit],
            "truncated": len(inventory) > limit,
            "download_hint": (
                "将需要的 level 文本原样传给 download_gfs 的 levels 参数；"
                "如果需要该变量全部层级，可不传 levels。"
            ),
            "references": [
                "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/",
                "https://registry.opendata.aws/noaa-gfs-bdp-pds/",
                "https://noaa-gfs-bdp-pds.s3.amazonaws.com",
            ],
        }
    except BaseException as e:
        if isinstance(e, asyncio.CancelledError):
            raise
        return {"status": "error", "message": f"GFS 库存查看失败：{e}"}


def _gfs_decision_to_dict(decision) -> dict:
    return {
        "requested_source": decision.requested_source,
        "selected_source": decision.selected_source,
        "available": decision.available,
        "reason": decision.reason,
        "nomads": _gfs_object_to_dict(decision.nomads),
        "aws": _gfs_object_to_dict(decision.aws),
    }


def _gfs_object_to_dict(item) -> dict:
    return {
        "source": item.source,
        "available": item.available,
        "base_url": item.base_url,
        "grib_url": item.grib_url,
        "idx_url": item.idx_url,
        "reason": item.reason,
        "status_code": item.status_code,
        "source_url": item.source_url,
    }

