"""CAMS ADS download tools."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import structlog

from aero.toolbox.config_access import find_config
from aero.toolbox.download_progress import download_progress_reporter
from aero.toolbox.paths import find_project_dir, short_path
from aero.toolbox.registry import register_tool

logger = structlog.get_logger()

EAC4_DATASET_ID = "cams-global-reanalysis-eac4"
FORECAST_DATASET_ID = "cams-global-atmospheric-composition-forecasts"


@register_tool(
    name="search_cams_variables",
    description=(
        "查询 CAMS ADS 数据集的可下载变量名。下载 CAMS 前如果变量不确定，"
        "必须先用本工具确认 ADS request 使用的准确 variable 值和 single/multi level 类型。"
        "支持中文/英文关键词和常见别名，如 臭氧、臭氧柱总量、pm2.5、aod、o3、tco3。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "dataset_id": {
                "type": "string",
                "enum": [EAC4_DATASET_ID, FORECAST_DATASET_ID],
                "description": "CAMS 数据集 ID。不填默认 EAC4 再分析。",
            },
            "query": {
                "type": "string",
                "description": (
                    "变量关键词、别名或 ADS 变量名，如 臭氧、pm2.5、o3、"
                    "total_column_ozone。"
                ),
            },
            "level_type": {
                "type": "string",
                "enum": ["single", "multi"],
                "description": (
                    "可选：single 表示单层/柱总量，multi 表示需要 pressure_levels "
                    "的多层变量。"
                ),
            },
        },
    },
)
async def search_cams_variables(
    dataset_id: str = EAC4_DATASET_ID,
    query: str = "",
    level_type: str = "",
) -> dict:
    """Search CAMS ADS form variables."""
    from aero.data.cams_variables import get_cams_variables
    from aero.data.cams_variables import search_cams_variables as _search

    if dataset_id not in {EAC4_DATASET_ID, FORECAST_DATASET_ID}:
        return {"status": "error", "message": f"不支持的 CAMS 数据集：{dataset_id}"}
    if level_type and level_type not in {"single", "multi"}:
        return {"status": "error", "message": "level_type 仅支持 single 或 multi。"}

    variables = await get_cams_variables(dataset_id)
    results = _search(variables, query=query, level_type=level_type)
    if not results:
        return {
            "status": "success",
            "found": False,
            "dataset_id": dataset_id,
            "query": query,
            "message": (
                "未找到匹配的 CAMS 变量。请换用更具体的关键词，例如 "
                "total column ozone、ozone、pm2.5、aod。"
            ),
            "references": _references(dataset_id),
        }
    return {
        "status": "success",
        "found": True,
        "dataset_id": dataset_id,
        "query": query,
        "count": len(results),
        "variables": [
            {
                "name": item["name"],
                "label": item["label"],
                "level_type": item["level_type"],
                "needs_pressure_levels": item["level_type"] == "multi",
                "group": item.get("group", ""),
            }
            for item in results[:80]
        ],
        "references": _references(dataset_id),
    }


@register_tool(
    name="download_cams",
    description=(
        "通过 Copernicus Atmosphere Data Store (ADS) 下载 CAMS 大气成分数据。"
        "支持 CAMS global reanalysis EAC4 和 CAMS global atmospheric composition forecasts。"
        "CAMS/ADS 使用单独的 ADS API 凭证，不要使用 ERA5/CDS 凭证。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "dataset_id": {
                "type": "string",
                "enum": [EAC4_DATASET_ID, FORECAST_DATASET_ID],
                "description": "CAMS 数据集 ID。不填默认 EAC4 再分析。",
            },
            "variables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "CAMS 变量名，如 total_column_ozone、particulate_matter_2.5um。",
            },
            "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD。"},
            "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD。"},
            "times": {
                "type": "array",
                "items": {"type": "string"},
                "description": "时次列表，如 ['00:00', '12:00']；不填默认全天逐小时。",
            },
            "leadtime_hours": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Forecast 数据集预报时效小时，如 [0, 24, 48]；EAC4 不需要。",
            },
            "pressure_levels": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "可选气压层 hPa。只有所选 CAMS 变量支持气压层时才填写。",
            },
            "area": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": "[north, west, south, east]，如 [60, 100, 20, 150]。",
            },
            "data_format": {
                "type": "string",
                "enum": ["netcdf", "grib"],
                "description": "输出格式，默认 netcdf。",
            },
        },
        "required": ["variables", "start_date", "end_date"],
        "additionalProperties": False,
    },
)
async def download_cams(
    variables: list[str],
    start_date: str,
    end_date: str,
    dataset_id: str = EAC4_DATASET_ID,
    times: list[str] | None = None,
    leadtime_hours: list[int] | None = None,
    pressure_levels: list[int] | None = None,
    area: list[float] | None = None,
    data_format: str = "netcdf",
) -> dict:
    """Download CAMS data from ADS using cdsapi-compatible requests."""
    from aero.adapters.cds_adapter import CDSAdapter, _detect_file_format
    from aero.agent.progress import emit_progress

    if dataset_id not in {EAC4_DATASET_ID, FORECAST_DATASET_ID}:
        return {"status": "error", "message": f"不支持的 CAMS 数据集：{dataset_id}"}
    if not variables:
        return {"status": "error", "message": "variables 不能为空。"}
    if data_format not in {"netcdf", "grib"}:
        return {"status": "error", "message": "data_format 仅支持 netcdf 或 grib。"}
    try:
        start = _parse_date(start_date, "start_date")
        end = _parse_date(end_date, "end_date")
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    if end < start:
        return {"status": "error", "message": "end_date 不能早于 start_date。"}
    if dataset_id == EAC4_DATASET_ID and leadtime_hours:
        return {"status": "error", "message": "EAC4 再分析数据不使用 leadtime_hours。"}
    if dataset_id == FORECAST_DATASET_ID and not leadtime_hours:
        leadtime_hours = (0, 24, 48)

    resolved_variables, variable_warnings, variable_records = await _resolve_variables(
        dataset_id,
        variables,
    )
    if missing := [
        warning for warning in variable_warnings if "未在 CAMS ADS 变量表中找到精确匹配" in warning
    ]:
        return {
            "status": "error",
            "message": (
                "CAMS 变量名不明确，不能直接提交下载请求。"
                f"{'；'.join(missing)}。请先调用 search_cams_variables 查询 ADS 变量名。"
            ),
            "suggested_tool": "search_cams_variables",
            "references": _references(dataset_id),
        }
    level_error = _validate_variable_level_combination(variable_records, pressure_levels or ())
    if level_error:
        return {
            "status": "error",
            "message": level_error,
            "suggested_tool": "search_cams_variables",
            "references": _references(dataset_id),
        }

    config = find_config()
    ads_cfg = config.credentials.ads
    if not ads_cfg.key:
        return {
            "status": "error",
            "message": (
                "ADS API key 未配置。CAMS 数据来自 Copernicus Atmosphere Data Store，"
                "请先配置 ADS Personal Access Token；它和 ERA5/CDS key 分开。"
            ),
            "suggested_tool": "check_ads_config",
        }

    request = _build_cams_request(
        dataset_id=dataset_id,
        variables=resolved_variables,
        start=start,
        end=end,
        times=times,
        leadtime_hours=leadtime_hours or (),
        pressure_levels=pressure_levels or (),
        area=area,
        data_format=data_format,
    )
    dest_path = _dest_path(dataset_id, resolved_variables, start, end, data_format)
    adapter = CDSAdapter(cds_url=ads_cfg.url, cds_key=ads_cfg.key)

    emit_progress(f"正在提交 CAMS ADS 请求：{dataset_id}")
    try:
        meta = await adapter.submit(
            dataset_id=dataset_id,
            variables=resolved_variables,
            year=start.year,
            month=start.month,
            day=start.day,
            area=area,
            data_format=data_format,
            request_overrides=request,
            dest_path=dest_path,
        )
    except Exception as exc:
        return _cams_submit_error_response(dataset_id, exc)

    emit_progress(f"正在下载 CAMS 文件：{short_path(dest_path)}")
    try:
        file_size = await adapter.fetch(
            download_url=meta["download_url"],
            dest_path=dest_path,
            on_progress=download_progress_reporter(),
            total_bytes=meta["total_bytes"],
        )
    except Exception as exc:
        return {
            "status": "error",
            "message": f"CAMS 数据下载失败：{exc}",
            "request_id": meta.get("request_id"),
            "download_url": meta.get("download_url"),
        }

    actual_file_format = _detect_file_format(dest_path)
    if actual_file_format == "grib" and dest_path.suffix != ".grib":
        grib_path = dest_path.with_suffix(".grib")
        if grib_path.exists():
            grib_path.unlink()
        dest_path.rename(grib_path)
        dest_path = grib_path
        file_size = dest_path.stat().st_size

    summary = {}
    try:
        import xarray as xr

        engine = "cfgrib" if actual_file_format == "grib" else None
        with xr.open_dataset(dest_path, engine=engine) as ds:
            summary = {
                name: {
                    "shape": list(ds[name].shape),
                    "dims": list(ds[name].dims),
                    "dtype": str(ds[name].dtype),
                }
                for name in resolved_variables
                if name in ds
            }
    except Exception as exc:
        logger.warning("cams.summary_failed", path=str(dest_path), error=str(exc))

    return {
        "status": "success",
        "message": "CAMS 数据下载完成。",
        "request_id": meta["request_id"],
        "file_path": short_path(dest_path),
        "file_size": file_size,
        "dataset_id": dataset_id,
        "variables": resolved_variables,
        "variable_warnings": variable_warnings,
        "time_range": {"start_date": start.isoformat(), "end_date": end.isoformat()},
        "times": request.get("time"),
        "leadtime_hours": request.get("leadtime_hour"),
        "pressure_levels": request.get("pressure_level"),
        "region": {"north": area[0], "west": area[1], "south": area[2], "east": area[3]}
        if area
        else None,
        "format": data_format,
        "actual_file_format": actual_file_format,
        "summary": summary,
        "references": _references(dataset_id),
    }


def _build_cams_request(
    *,
    dataset_id: str,
    variables: list[str],
    start: date,
    end: date,
    times: list[str] | None,
    leadtime_hours: tuple[int, ...],
    pressure_levels: tuple[int, ...],
    area: list[float] | None,
    data_format: str,
) -> dict:
    request: dict = {
        "variable": variables,
        "date": f"{start.isoformat()}/{end.isoformat()}",
        "time": times or [f"{hour:02d}:00" for hour in range(24)],
        "data_format": "netcdf_zip" if data_format == "netcdf" else "grib",
    }
    if dataset_id == FORECAST_DATASET_ID:
        request["type"] = ["forecast"]
        request["leadtime_hour"] = [str(hour) for hour in leadtime_hours]
    if pressure_levels:
        request["pressure_level"] = [str(level) for level in pressure_levels]
    if area:
        request["area"] = area
    return request


async def _resolve_variables(
    dataset_id: str,
    variables: list[str],
) -> tuple[list[str], list[str], list[dict[str, str]]]:
    from aero.data.cams_variables import get_cams_variables, resolve_cams_variable_names

    available = await get_cams_variables(dataset_id)
    return resolve_cams_variable_names(variables, available)


def _validate_variable_level_combination(
    variable_records: list[dict[str, str]],
    pressure_levels: tuple[int, ...],
) -> str:
    single_level = [
        item["name"] for item in variable_records if item.get("level_type") == "single"
    ]
    multi_level = [item["name"] for item in variable_records if item.get("level_type") == "multi"]
    if pressure_levels and single_level:
        return (
            "CAMS 变量层次组合不合法："
            f"{', '.join(single_level)} 是 single level 变量，不能同时请求 pressure_levels。"
            "如果你要柱总量，请去掉 pressure_levels；如果你要某一气压层浓度，"
            "请改用对应 multi level 变量。"
        )
    if multi_level and not pressure_levels:
        return (
            "CAMS 变量层次组合不完整："
            f"{', '.join(multi_level)} 是 multi level 变量，需要指定 pressure_levels。"
            "如果你要柱总量，请改用 total_column_* 变量。"
        )
    return ""


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} 必须使用 YYYY-MM-DD 格式") from exc


def _dest_path(
    dataset_id: str,
    variables: list[str],
    start: date,
    end: date,
    data_format: str,
) -> Path:
    ext = ".grib" if data_format == "grib" else ".nc"
    dataset_part = _safe_filename_part(dataset_id)
    var_part = "_".join(_safe_filename_part(variable) for variable in variables[:3])
    if len(variables) > 3:
        var_part += f"_plus{len(variables) - 3}"
    date_part = start.strftime("%Y%m%d")
    if end != start:
        date_part += f"_{end:%Y%m%d}"
    config = find_config()
    return find_project_dir() / config.output.data_dir / (
        f"cams_{dataset_part}_{var_part}_{date_part}{ext}"
    )


def _safe_filename_part(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    text = re.sub(r"_+", "_", text).strip("._-")
    return text or "unknown"


def _references(dataset_id: str) -> list[str]:
    return [
        "https://ads.atmosphere.copernicus.eu/",
        _dataset_download_url(dataset_id),
    ]


def _dataset_download_url(dataset_id: str) -> str:
    return f"https://ads.atmosphere.copernicus.eu/datasets/{dataset_id}?tab=download"


def _cams_submit_error_response(dataset_id: str, exc: Exception) -> dict:
    error_text = str(exc)
    lower_error = error_text.lower()
    references = _references(dataset_id)

    if _looks_like_request_schema_error(lower_error):
        return {
            "status": "error",
            "message": (
                "CAMS ADS 提交失败：请求参数不被该数据集接受。"
                f"原始错误：{error_text}。请根据数据集下载页的 Request 模板调整变量、"
                "层次、时间或字段名。"
            ),
            "references": references,
        }

    if _looks_like_terms_or_auth_error(lower_error):
        terms_url = _dataset_download_url(dataset_id)
        return {
            "status": "error",
            "message": (
                f"CAMS ADS 提交失败：{error_text}。如果是首次使用该数据集，"
                f"请先打开这个直达链接并接受 Terms of Use：{terms_url}"
            ),
            "terms_url": terms_url,
            "references": references,
        }

    return {
        "status": "error",
        "message": f"CAMS ADS 提交失败：{error_text}",
        "references": references,
    }


def _looks_like_request_schema_error(lower_error: str) -> bool:
    return any(
        marker in lower_error
        for marker in (
            "invalid key name",
            "invalid request",
            "bad request",
            "400 client error",
            "unknown field",
            "unexpected field",
        )
    )


def _looks_like_terms_or_auth_error(lower_error: str) -> bool:
    return any(
        marker in lower_error
        for marker in (
            "terms",
            "licence",
            "license",
            "unauthorized",
            "forbidden",
            "401",
            "403",
        )
    )
