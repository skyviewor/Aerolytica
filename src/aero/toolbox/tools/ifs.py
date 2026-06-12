"""ECMWF IFS/AIFS forecast schedule, availability, search, and download tools."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from aero.toolbox.download_progress import download_progress_reporter
from aero.toolbox.paths import find_project_dir, short_path
from aero.toolbox.registry import register_tool


# ---------------------------------------------------------------------------
# IFS (ECMWF Integrated Forecasting System) tools
# ---------------------------------------------------------------------------


@register_tool(
    name="get_ifs_forecast_schedule",
    description=(
        "根据 ECMWF IFS/AIFS 官方预报时效间隔，把用户请求的起报后时间窗口解析成应下载的 steps。"
        "IFS 预报时效取决于起报时次和预报系统："
        "oper/wave 00z/12z 提供 0-144h 每 3h + 150-240h 每 6h；"
        "oper/wave 06z/18z 提供 0-90h 每 3h；"
        "enfo/waef 00z/12z 提供 0-144h 每 3h + 150-360h 每 6h；"
        "enfo/waef 06z/18z 提供 0-144h 每 3h；"
        "AIFS 全时次提供 0-360h 每 6h。"
        "用户说下载未来 N 小时、某段预报时效时，应先调用此工具，不要默认每 3 小时。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "start_step": {
                "type": "integer",
                "description": "起始预报步长（小时），默认 0。例如从起报时刻开始就是 0。",
            },
            "end_step": {
                "type": "integer",
                "description": "结束预报步长（小时），包含端点。例如未来 12 小时传 12。",
            },
            "duration_hours": {
                "type": "integer",
                "description": "持续小时数；如果给了 end_step，可不传。未来 12 小时通常传 12。",
            },
            "cycle": {
                "type": "string",
                "enum": ["00", "06", "12", "18"],
                "description": "起报时次，用于确定可用时效范围。默认 00。",
            },
            "stream": {
                "type": "string",
                "enum": ["oper", "wave", "enfo", "waef"],
                "description": "预报系统，默认 oper。enfo/waef 有更长的预报时效。",
            },
            "model": {
                "type": "string",
                "enum": ["ifs", "aifs-single", "aifs-ens"],
                "description": "预报模型，默认 ifs。AIFS 时效为 0-360h 每 6h。",
            },
        },
    },
)
async def get_ifs_forecast_schedule(
    start_step: int = 0,
    end_step: int | None = None,
    duration_hours: int | None = None,
    cycle: str = "00",
    stream: str = "oper",
    model: str = "ifs",
) -> dict:
    from aero.adapters.ifs_adapter import ifs_forecast_segments, ifs_forecast_steps_for_range

    try:
        steps = ifs_forecast_steps_for_range(
            start_step=start_step,
            end_step=end_step,
            duration_hours=duration_hours,
            cycle=cycle,
            stream=stream,
            model=model,
        )
    except Exception as e:
        return {"status": "error", "message": f"预报时效解析失败：{e}"}

    if not steps:
        return {
            "status": "error",
            "message": "这个时间窗口内没有匹配的预报输出步长。",
            "start_step": start_step,
            "end_step": end_step,
            "duration_hours": duration_hours,
            "cycle": cycle,
            "stream": stream,
            "model": model,
            "segments": ifs_forecast_segments(cycle, stream=stream, model=model),
        }
    return {
        "status": "success",
        "message": (
            f"预报步长已解析：step={steps[0]}h-step={steps[-1]}h，共 {len(steps)} 个文件。"
        ),
        "download_hint": (
            "把 steps 原样传给 download_ifs；不要跨时次套用固定间隔。"
            "下载或可用性检查会继续确认具体远端文件是否存在。"
        ),
        "steps": steps,
        "count": len(steps),
        "cycle": cycle,
        "stream": stream,
        "model": model,
        "segments": ifs_forecast_segments(cycle, stream=stream, model=model),
        "availability_check_recommended": True,
    }


@register_tool(
    name="download_ifs",
    description=(
        "从 ECMWF Open Data 下载 IFS/AIFS 全球预报。支持大气预报（oper）、海浪预报（wave）、"
        "集合预报（enfo/waef）。默认优先使用 ECMWF 官方门户；"
        "官网没有较早时次时，自动尝试 AWS OpenData 历史归档。"
        "支持根据官方 .index 文件按变量、层级类型和层级分块下载，只保存命中的 GRIB2 message。"
        "v1 不支持经纬度裁剪；variables 使用 ECMWF short name，如 2t、tp、z、10u、10v。"
        "如果用户给的是时间窗口或持续小时数，先用 get_ifs_forecast_schedule 解析 steps，"
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
            "model": {
                "type": "string",
                "enum": ["ifs", "aifs-single", "aifs-ens"],
                "description": (
                    "预报模型：ifs 物理模型、aifs-single AI 单次、aifs-ens AI 集合。"
                    "默认 ifs。enfo/waef 在 aifs-ens 下文件类型为 pf/cf，在 ifs 下为组合文件 ef"
                ),
            },
            "stream": {
                "type": "string",
                "enum": ["oper", "wave", "enfo", "waef"],
                "description": (
                    "预报系统：oper 大气 HRES、wave 海浪 HRES、"
                    "enfo 大气集合、waef 海浪集合。默认 oper。"
                    "enfo/waef 仅在 00z/12z 可用"
                ),
            },
            "steps": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "预报步长列表（小时），如 [0, 6, 12]；每个步长输出一个 .grib2 文件",
            },
            "variables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "ECMWF short name 列表，如 ['2t', 'tp', 'z', '10u', '10v']",
            },
            "levtype": {
                "type": "string",
                "enum": ["sfc", "pl", "sol"],
                "description": "层级类型：sfc 地表、pl 等压面、sol 土壤层。不填则匹配全部层级类型",
            },
            "levels": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "可选层级列表。pl 时如 ['500', '850']（hPa），sol 时如 ['1', '2']。"
                    "不填则下载指定变量在该 levtype 下的全部层级"
                ),
            },
            "source": {
                "type": "string",
                "enum": ["auto", "ecmwf", "aws", "google"],
                "description": ("数据来源，默认 auto：优先官网 → AWS → Google Cloud 自动回退"),
            },
            "number": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "集合成员编号列表，如 [1, 10, 20]。仅对 enfo/waef 有效。不填则下载全部成员"
                ),
            },
        },
        "required": ["date", "cycle", "steps", "variables"],
    },
)
async def download_ifs(
    date: str,
    cycle: str,
    steps: list[int],
    variables: list[str],
    levtype: str | None = None,
    levels: list[str] | None = None,
    source: str = "auto",
    stream: str = "oper",
    model: str = "ifs",
    number: list[int] | None = None,
) -> dict:
    from aero.adapters.ifs_adapter import (
        DEFAULT_TYPE_BY_MODEL_STREAM,
        IFSAdapter,
        _build_request_id,
    )
    from aero.data.download_store import CDSDownloadStore
    from aero.data.ifs_availability import resolve_ifs_source

    project_dir = find_project_dir()
    store = CDSDownloadStore(project_dir / "aero_downloads.db")

    typ = DEFAULT_TYPE_BY_MODEL_STREAM.get((model, stream), "fc")

    try:
        results = []
        for step in steps:
            decision = await resolve_ifs_source(
                date=date,
                cycle=cycle,
                step=step,
                stream=stream,
                typ=typ,
                model=model,
                source=source,
            )
            if not decision.available or decision.selected is None:
                return {
                    "status": "error",
                    "message": f"下载失败：{decision.reason}",
                    "date": decision.date,
                    "cycle": decision.cycle,
                    "step": decision.step,
                    "stream": decision.stream,
                    "model": decision.model,
                    "requested_source": decision.requested_source,
                    "availability": _ifs_decision_to_dict(decision),
                }
            if decision.selected_source == "aws" and decision.requested_source == "auto":
                from aero.agent.progress import emit_progress as _emit

                _emit("官网没有这个时次，正在尝试从 AWS 历史归档获取")
            adapter = IFSAdapter(base_url=decision.selected.base_url)
            result = await adapter.download_one(
                date=date,
                cycle=cycle,
                step=step,
                variables=variables,
                levtype=levtype,
                levels=levels,
                stream=stream,
                typ=typ,
                model=model,
                numbers=number,
                on_progress=download_progress_reporter(),
            )
            results.append(
                (
                    replace(result, source=decision.selected_source or "unknown"),
                    decision,
                )
            )
    except BaseException as e:
        if isinstance(e, asyncio.CancelledError):
            raise
        return {"status": "error", "message": f"下载失败：{e}"}

    files = []
    total_bytes = 0
    all_missing = []
    sources_used = []
    dataset_id = f"{model}-0p25-{stream}-{typ}"
    for result, decision in results:
        request_id = _build_request_id(
            date=date,
            cycle=cycle,
            step=result.step,
            variables=variables,
            levtype=levtype,
            levels=levels,
            stream=stream,
            typ=typ,
            model=model,
        )
        selected = [
            {
                "param": entry.param,
                "levtype": entry.levtype,
                "levelist": entry.levelist,
                "step": entry.step,
                "range": entry.range_header,
            }
            for entry in result.selected_entries
        ]
        notes = {
            "stream": stream,
            "type": typ,
            "model": model,
            "requested_source": source,
            "data_source": result.source,
            "availability": _ifs_decision_to_dict(decision),
            "index_url": result.index_url,
            "grib_url": result.grib_url,
            "selected_messages": len(result.selected_entries),
            "missing": result.missing,
            "range_total_bytes": result.downloaded_bytes,
        }
        row_id = store.insert(
            source="ifs",
            request_id=request_id,
            dataset_id=dataset_id,
            variables=[v.lower() for v in variables],
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
                "step": result.step,
                "source_used": result.source,
                "file_path": short_path(result.file_path),
                "file_size": result.downloaded_bytes,
                "index_url": result.index_url,
                "selected_messages": len(result.selected_entries),
                "selected": selected,
                "missing": result.missing,
            }
        )

    return {
        "status": "success",
        "source": "ifs",
        "dataset_id": dataset_id,
        "date": date,
        "cycle": cycle,
        "stream": stream,
        "model": model,
        "type": typ,
        "requested_source": source,
        "sources_used": sorted(set(sources_used)),
        "variables": [v.lower() for v in variables],
        "levtype": levtype,
        "levels": levels,
        "files": files,
        "total_files": len(files),
        "total_bytes": total_bytes,
        "missing": all_missing,
        "note": "IFS 使用官方 .index + HTTP Range 分块下载，不做经纬度裁剪。",
        "references": [
            "https://data.ecmwf.int/forecasts/",
            "https://registry.opendata.aws/ecmwf-forecasts/",
            "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
            "https://storage.googleapis.com/ecmwf-open-data",
        ],
    }


@register_tool(
    name="check_ifs_availability",
    description=(
        "检查 ECMWF IFS 官网、AWS OpenData 和 Google Cloud 当前支持哪些日期/时次。"
        "不传 date 时返回三个来源的可用范围；传 date/cycle 时检查目标步长是否可下载。"
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
            "step": {
                "type": "integer",
                "description": "预报步长（小时），默认 0",
            },
            "refresh": {
                "type": "boolean",
                "description": "是否绕过本地缓存重新查询目录",
            },
        },
    },
)
async def check_ifs_availability(
    date: str | None = None,
    cycle: str | None = None,
    step: int = 0,
    refresh: bool = False,
) -> dict:
    from aero.data.ifs_availability import (
        AWS_REGISTRY_URL,
        cache_path,
        get_ifs_availability,
        resolve_ifs_source,
    )

    try:
        if date:
            if not cycle:
                return {
                    "status": "error",
                    "message": "检查指定日期时需要同时提供起报时次，例如 00、06、12 或 18。",
                }
            decision = await resolve_ifs_source(
                date=date,
                cycle=cycle,
                step=step,
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
                    "step": decision.step,
                    "stream": decision.stream,
                    "type": decision.typ,
                },
                "availability": _ifs_decision_to_dict(decision),
                "references": [
                    "https://data.ecmwf.int/forecasts/",
                    AWS_REGISTRY_URL,
                    "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
                    "https://storage.googleapis.com/ecmwf-open-data",
                ],
            }

        summary = await get_ifs_availability(refresh=refresh)
        return {
            "status": "success",
            "mode": "range",
            "ecmwf": summary["ecmwf"],
            "aws": summary["aws"],
            "cache_path": str(cache_path()),
            "references": [
                "https://data.ecmwf.int/forecasts/",
                AWS_REGISTRY_URL,
                "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
                "https://storage.googleapis.com/ecmwf-open-data",
            ],
        }
    except BaseException as e:
        if isinstance(e, asyncio.CancelledError):
            raise
        return {"status": "error", "message": f"IFS 可用性检查失败：{e}"}


@register_tool(
    name="search_ifs_variables",
    description=(
        "搜索 ECMWF IFS 开源数据中可用的要素（ECMWF short name）。"
        "数据来自 IFS 开源数据官方参数列表，支持中文/英文关键词和 ECMWF short name。"
        "使用时先确定要素是否可用，返回的 param 即为 download_ifs 中使用的 variables。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "变量关键词或 ECMWF short name，如 2t、temperature、温度、降水、风",
            },
        },
    },
)
async def search_ifs_variables(keyword: str | None = None) -> dict:
    from aero.data.ifs_params import LEVTYPE_NAMES, search_ifs_parameters

    result = search_ifs_parameters(keyword)
    result["levtype_hint"] = {**LEVTYPE_NAMES}
    result["references"] = [
        "https://codes.ecmwf.int/parameter-database/api/v1",
        "https://data.ecmwf.int/forecasts/",
    ]
    return result




def _ifs_decision_to_dict(decision) -> dict:
    return {
        "requested_source": decision.requested_source,
        "selected_source": decision.selected_source,
        "available": decision.available,
        "reason": decision.reason,
        "ecmwf": _ifs_object_to_dict(decision.ecmwf),
        "aws": _ifs_object_to_dict(decision.aws),
        "google": _ifs_object_to_dict(decision.google),
    }


def _ifs_object_to_dict(item) -> dict:
    return {
        "source": item.source,
        "available": item.available,
        "base_url": item.base_url,
        "grib_url": item.grib_url,
        "index_url": item.index_url,
        "reason": item.reason,
        "status_code": item.status_code,
        "source_url": item.source_url,
    }

