"""NCEP GEFS schedule, availability, search, lookup, and download tools."""

from __future__ import annotations

from meteora.toolbox.download_progress import download_progress_reporter
from meteora.toolbox.paths import find_project_dir, short_path
from meteora.toolbox.registry import register_tool


# ── GEFS Tools ──


@register_tool(
    name="get_gefs_forecast_schedule",
    description=(
        "解析 GEFS 集合预报时效。根据用户给的时间窗口（起止小时或持续时长），"
        "返回 GEFS 实际可用的 forecast_hours 列表。\n"
        "下载 GEFS 数据前需要先用此工具解析时效，获得正确的 forecast_hours 再传给 download_gefs。\n"
        "注意：默认查询控制成员预报时效；扰动成员时效与控制成员一致。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "start_hour": {
                "type": "integer",
                "description": "起始预报时效（小时），默认 0",
            },
            "end_hour": {
                "type": "integer",
                "description": "结束预报时效（含）。与 duration_hours 二选一",
            },
            "duration_hours": {
                "type": "integer",
                "description": "预报持续时长。与 end_hour 二选一。如 240 表示从 start_hour 开始的 240 小时",
            },
            "product": {
                "type": "string",
                "description": "产品 ID，默认 gefs.0p50",
                "enum": ["gefs.0p50", "gefs.0p50b", "gefs.0p25"],
            },
            "date": {
                "type": "string",
                "description": "日期 YYYYMMDD。可选，默认最新日期",
            },
        },
    },
)
def get_gefs_forecast_schedule(
    start_hour: int = 0,
    end_hour: int | None = None,
    duration_hours: int | None = None,
    product: str = "gefs.0p50",
    date: str | None = None,
    **kwargs,
) -> dict:
    from meteora.data.gefs_availability import (
        MAX_FORECAST_HOUR,
        gefs_forecast_hours_for_range,
    )

    if end_hour is None and duration_hours is None:
        end_hour = start_hour
    if end_hour is not None and end_hour < 0:
        end_hour = 0
    if start_hour > MAX_FORECAST_HOUR:
        raise ValueError(f"start_hour 最大 {MAX_FORECAST_HOUR}")
    if end_hour is not None and end_hour > MAX_FORECAST_HOUR:
        raise ValueError(f"end_hour 最大 {MAX_FORECAST_HOUR}")

    result = gefs_forecast_hours_for_range(
        start_hour=start_hour,
        end_hour=end_hour,
        duration_hours=duration_hours,
        product=product,
        date=date,
    )
    result["download_hint"] = (
        "将返回的 forecast_hours 列表直接传给 download_gefs 的 forecast_hours 参数。"
        '默认只下载控制成员 c00；如需更多成员，传入 members 参数（如 members=["c00","p01","p02"]）。'
    )
    return result


@register_tool(
    name="check_gefs_availability",
    description=(
        "检查 GEFS（全球集合预报）某时次在 NOMADS 和 AWS OpenData 的可用性。\n"
        "不传 date 则返回两个源的可用日期范围。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "日期 YYYYMMDD。可选，不传则返回可用日期范围",
            },
            "cycle": {
                "type": "string",
                "description": "起报时次 00/06/12/18",
                "enum": ["00", "06", "12", "18"],
            },
            "product": {
                "type": "string",
                "description": "产品 ID，默认 gefs.0p50",
                "enum": ["gefs.0p50", "gefs.0p50b", "gefs.0p25"],
            },
            "forecast_hour": {
                "type": "integer",
                "description": "预报时效小时",
            },
            "member": {
                "type": "string",
                "description": "成员 ID，默认 c00",
            },
            "source": {
                "type": "string",
                "description": "数据源 auto（默认）/ nomads / aws",
                "enum": ["auto", "nomads", "aws"],
            },
        },
    },
)
async def check_gefs_availability(
    date: str | None = None,
    cycle: str = "00",
    product: str = "gefs.0p50",
    forecast_hour: int = 0,
    member: str = "c00",
    source: str = "auto",
    **kwargs,
) -> dict:
    from meteora.data.gefs_availability import (
        get_gefs_availability,
        resolve_gefs_source,
    )

    if date is None:
        status = await get_gefs_availability()
        return {
            "status": "success",
            "nomads": status.get("nomads"),
            "aws": status.get("aws"),
            "cached_at": status.get("cached_at"),
        }

    decision = await resolve_gefs_source(
        date=date,
        cycle=cycle,
        product=product,
        forecast_hour=forecast_hour,
        member=member,
        source=source,
    )

    def _avail_to_dict(obj) -> dict:
        return {
            "source": obj.source,
            "available": obj.available,
            "base_url": obj.base_url,
            "grib_url": obj.grib_url,
            "idx_url": obj.idx_url,
            "reason": obj.reason,
            "status_code": obj.status_code,
        }

    return {
        "status": "success",
        "requested_source": decision.requested_source,
        "selected_source": decision.selected_source,
        "available": decision.available,
        "date": decision.date,
        "cycle": decision.cycle,
        "product": decision.product,
        "forecast_hour": decision.forecast_hour,
        "member": decision.member,
        "nomads": _avail_to_dict(decision.nomads),
        "aws": _avail_to_dict(decision.aws),
        "reason": decision.reason,
    }


@register_tool(
    name="download_gefs",
    description=(
        "下载 NCEP GEFS（全球集合预报系统）GRIB2 数据。\n"
        "支持控制成员（c00）和扰动成员（p01-p30）的选定变量和气压层下载。\n"
        "GEFS 使用 .idx 索引文件按变量/层次/成员精确匹配 GRIB message，"
        "通过 HTTP Range 请求只下载需要的 message，不下载整个 GRIB2 文件。\n"
        "无需 API key。默认从 NOMADS 官网下载，如不可用自动回退到 AWS OpenData。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "日期 YYYYMMDD",
            },
            "cycle": {
                "type": "string",
                "description": "起报时次",
                "enum": ["00", "06", "12", "18"],
            },
            "forecast_hours": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "预报时效列表。先用 get_gefs_forecast_schedule 解析正确的时效",
            },
            "variables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "GRIB2 变量短名列表。如 TMP、HGT、UGRD、VGRD。用 search_gefs_variables 查找可用变量",
            },
            "levels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "气压层列表（如 ['500 mb', '850 mb']）。不传则下载所有可用层次",
            },
            "members": {
                "type": "array",
                "items": {"type": "string"},
                "description": "成员 ID 列表。默认 ['c00']（仅控制成员）。"
                "支持 c00（控制）、p01-p30（扰动）",
            },
            "product": {
                "type": "string",
                "description": "产品 ID，默认 gefs.0p50",
                "enum": ["gefs.0p50", "gefs.0p50b", "gefs.0p25"],
            },
            "source": {
                "type": "string",
                "description": "数据源。默认 auto（NOMADS 优先，不可用则 AWS）",
                "enum": ["auto", "nomads", "aws"],
            },
        },
        "required": ["date", "cycle", "forecast_hours", "variables"],
    },
)
async def download_gefs(
    date: str,
    cycle: str,
    forecast_hours: list[int],
    variables: list[str],
    levels: list[str] | None = None,
    members: list[str] | None = None,
    product: str = "gefs.0p50",
    source: str = "auto",
    **kwargs,
) -> dict:
    import json

    from meteora.adapters.gefs_adapter import (
        GEFSAdapter,
        build_request_id,
        dataset_id_for_product,
    )
    from meteora.data.download_store import CDSDownloadStore
    from meteora.data.gefs_availability import (
        resolve_gefs_source,
    )

    project_dir = find_project_dir()
    store = CDSDownloadStore(project_dir / "meteora_downloads.db")

    forecast_hours = sorted(set(forecast_hours))
    variables = [v.upper() for v in variables]
    if members is None:
        members = ["c00"]
    members = sorted(set(members))
    if levels:
        levels = sorted(set(levels))

    date = date.strip()
    cycle = cycle.strip()

    all_missing = []
    sources_used: set[str] = set()
    files = []
    total_bytes = 0
    request_id = ""

    for fhour in forecast_hours:
        for member in members:
            decision = await resolve_gefs_source(
                date=date,
                cycle=cycle,
                product=product,
                forecast_hour=fhour,
                member=member,
                source=source,
            )
            if not decision.available:
                all_missing.append(
                    {
                        "forecast_hour": fhour,
                        "member": member,
                        "reason": decision.reason,
                    }
                )
                continue

            sources_used.add(decision.selected_source or "")
            base_url = (
                decision.nomads.base_url
                if decision.selected_source == "nomads"
                else decision.aws.base_url
            )
            adapter = GEFSAdapter(base_url=base_url)

            if decision.selected_source == "aws" and source == "auto":
                from meteora.agent.progress import emit_progress

                emit_progress(
                    f"GEFS NOMADS 不可用，已自动切换到 AWS OpenData：f{fhour:03d} 成员={member}"
                )

            try:
                result = await adapter.download_one(
                    date=date,
                    cycle=cycle,
                    forecast_hour=fhour,
                    variables=variables,
                    member=member,
                    product=product,
                    levels=levels,
                    on_progress=download_progress_reporter(),
                )
            except Exception as e:
                all_missing.append(
                    {
                        "forecast_hour": fhour,
                        "member": member,
                        "reason": str(e),
                    }
                )
                continue

            request_id = build_request_id(
                date=date,
                cycle=cycle,
                member=member,
                forecast_hour=fhour,
                variables=variables,
                levels=levels,
                product=product,
            )
            notes = {
                "product": product,
                "member": member,
                "requested_source": source,
                "data_source": decision.selected_source,
                "availability": {
                    "nomads": {
                        "available": decision.nomads.available,
                        "reason": decision.nomads.reason,
                    },
                    "aws": {
                        "available": decision.aws.available,
                        "reason": decision.aws.reason,
                    },
                },
                "idx_url": result.idx_url,
                "grib_url": result.grib_url,
                "selected_messages": len(result.selected_entries),
                "missing": result.missing,
            }

            row_id = store.insert(
                source="gefs",
                request_id=request_id,
                dataset_id=dataset_id_for_product(product),
                variables=variables,
                file_path=short_path(result.file_path),
                file_size=result.downloaded_bytes,
                download_url=result.grib_url,
                status="completed_with_file",
                total_bytes=result.downloaded_bytes,
                downloaded_bytes=result.downloaded_bytes,
                data_format="grib2",
                notes=json.dumps(notes, ensure_ascii=False),
            )

            files.append(
                {
                    "download_id": row_id,
                    "request_id": request_id,
                    "forecast_hour": result.forecast_hour,
                    "member": result.member,
                    "source_used": decision.selected_source,
                    "file_path": short_path(result.file_path),
                    "file_size": result.downloaded_bytes,
                    "idx_url": result.idx_url,
                    "selected_messages": len(result.selected_entries),
                    "selected": [
                        {"variable": e.variable, "level": e.level} for e in result.selected_entries
                    ],
                    "missing": result.missing,
                }
            )
            total_bytes += result.downloaded_bytes

    return {
        "status": "success",
        "source": "gefs",
        "dataset_id": dataset_id_for_product(product),
        "date": date,
        "cycle": cycle,
        "product": product,
        "members": members,
        "requested_source": source,
        "sources_used": sorted(sources_used),
        "variables": variables,
        "levels": levels,
        "files": files,
        "total_files": len(files),
        "total_bytes": total_bytes,
        "missing": all_missing,
        "note": (
            "GEFS 全球集合预报 .idx 索引 + HTTP Range 分块下载，"
            "默认仅下载控制成员 c00；需要集合成员时传入 members 参数。"
        ),
        "references": [
            "https://www.nco.ncep.noaa.gov/pmb/products/gens/",
            "https://registry.opendata.aws/noaa-gefs/",
        ],
    }


@register_tool(
    name="search_gefs_variables",
    description=(
        "搜索 GEFS（全球集合预报）GRIB2 可用要素。"
        "根据关键词或要素类型查找 GEFS 产品中包含的气象变量。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "搜索关键词，如 '温度'、'temperature'、'风'、'TMP'",
            },
            "data_type": {
                "type": "string",
                "description": "要素类型：高空（气压层）/ 地面 / 全部。默认全部",
                "enum": ["高空", "地面", "全部"],
            },
        },
    },
)
async def search_gefs_variables(
    keyword: str = "",
    data_type: str = "全部",
    **kwargs,
) -> dict:
    from meteora.data.gfs_params import get_gfs_parameters, search_gfs_parameters

    keyword_lower = keyword.strip().lower() if keyword else ""

    try:
        params = await get_gfs_parameters()
    except Exception:
        params = []

    if keyword_lower:
        results = search_gfs_parameters(params, keyword=keyword_lower)[:200]
    else:
        results = params

    if data_type != "全部":
        filtered = []
        for p in results:
            lt = (p.get("level_type") or "").lower()
            if data_type == "高空" and lt in ("高空", "isobaricinhpa", "pressure"):
                filtered.append(p)
            elif data_type == "地面" and lt in ("地面", "surface", "云层"):
                filtered.append(p)
        results = filtered

    if not results:
        return {
            "status": "success",
            "found": False,
            "keyword": keyword,
            "data_type": data_type,
            "message": (
                f"GEFS 中没有找到关键词 '{keyword}' 匹配的要素。"
                "建议用英文关键词或短名检索（如 temperature、wind、hgt）。"
            ),
            "suggestion": (
                "可以尝试常见变量：TMP（温度）、HGT（位势高度）、"
                "UGRD（U风）、VGRD（V风）、RH（相对湿度）、"
                "PRMSL（海平面气压）、ABSV（绝对涡度）"
            ),
        }

    return {
        "status": "success",
        "found": True,
        "keyword": keyword,
        "data_type": data_type,
        "count": len(results),
        "variables": [
            {
                "short_name": p.get("short_name", ""),
                "name": p.get("name", ""),
                "param_id": p.get("param_id", ""),
                "units": p.get("units", ""),
                "level_type": p.get("level_type", ""),
            }
            for p in results[:50]
        ],
        "note": "GEFS 与 GFS 共享同一套 GRIB2 参数表，变量名通用。",
        "references": [
            "https://www.nco.ncep.noaa.gov/pmb/products/gens/",
            "https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2.shtml",
        ],
    }


@register_tool(
    name="lookup_gefs_parameter",
    description=(
        "查找 GEFS GRIB2 要素的官方定义、单位和典型用法。用户问'XXX是什么变量'时可用此工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "param": {
                "type": "string",
                "description": "GRIB2 短名或参数名，如 'TMP'、'HGT'、'UGRD'、'温度'",
            },
        },
        "required": ["param"],
    },
)
async def lookup_gefs_parameter(param: str, **kwargs) -> dict:
    from meteora.data.gfs_params import lookup_gfs_parameters

    try:
        result = await lookup_gfs_parameters(query=param, limit=5)
    except Exception as e:
        return {
            "status": "success",
            "found": False,
            "query": param,
            "message": str(e),
            "suggestion": (
                "尝试用英文短名查询（如 TMP、HGT、UGRD、VGRD、RH、PRMSL）"
                "或用 search_gefs_variables 搜索。"
            ),
        }

    if not result.get("found"):
        return {
            "status": "success",
            "found": False,
            "query": param,
            "message": f"未找到 '{param}' 的 GEFS 要素定义。GEFS 与 GFS 共享同一套参数表。",
            "suggestion": (
                "尝试用英文短名查询（如 TMP、HGT、UGRD、VGRD、RH、PRMSL）"
                "或用 search_gefs_variables 搜索。"
            ),
        }

    return {
        "status": "success",
        "found": True,
        "query": param,
        "count": result.get("count", len(result.get("parameters", []))),
        "parameters": result.get("parameters", []),
        "note": "GEFS 与 GFS 共享同一套 GRIB2 参数表。",
        "references": [
            "https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2.shtml",
        ],
    }


