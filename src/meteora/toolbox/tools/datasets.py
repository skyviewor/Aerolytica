"""Unified dataset catalogue tools."""

from pathlib import Path

import httpx

from meteora.core.debug_log import debug_exception
from meteora.toolbox.paths import find_project_dir, short_path
from meteora.toolbox.registry import register_tool


def _default_dataset_output_dir() -> Path:
    project_dir = find_project_dir()
    if (project_dir / "meteora.yaml").exists():
        return project_dir / "data"
    return project_dir / "lab" / "data"


@register_tool(
    name="search_datasets",
    description=(
        "查询 Meteora 统一数据集目录。所有内置支持的数据集都收录在这里；"
        "回答支持哪些数据或准备下载任何数据前，先调用本工具解析准确的数据集和下载路由。"
        "找到候选项后先调用 describe_dataset 确认时间范围、下载粒度、认证和裁剪限制。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "数据集、变量或用途关键词，如 降水、precipitation、CHIRPS。",
            },
            "domain": {
                "type": "string",
                "description": "可选领域，如 observations、forecast、satellite。",
            },
            "provider": {
                "type": "string",
                "description": "可选 Provider 名称或 ID。",
            },
            "requires_auth": {
                "type": "boolean",
                "description": "可选，仅返回需要或不需要认证的数据集。",
            },
        },
    },
)
async def search_datasets(
    query: str = "",
    domain: str = "",
    provider: str = "",
    requires_auth: bool | None = None,
) -> dict:
    from meteora.datasets import get_dataset_catalog

    datasets = get_dataset_catalog().search(
        query,
        domain=domain,
        provider=provider,
        requires_auth=requires_auth,
    )
    return {
        "status": "success",
        "count": len(datasets),
        "datasets": [
            {
                "dataset_id": item.dataset_id,
                "name": item.name,
                "provider": item.provider_name,
                "domain": item.domain,
                "description": item.description,
                "variables": [variable.name for variable in item.variables],
                "temporal_coverage": item.temporal_coverage,
                "spatial_resolution": item.spatial_resolution,
                "temporal_resolution": item.temporal_resolution,
                "requires_auth": item.requires_auth,
                "download_granularity": item.download_granularity,
                "download_tool": item.download_tool,
            }
            for item in datasets
        ],
    }


@register_tool(
    name="describe_dataset",
    description=(
        "查询统一目录中某个数据集的完整能力和限制。"
        "下载前必须用它确认变量、时间范围、下载粒度、断点续传以及是否支持服务端裁剪。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "dataset_id": {"type": "string", "description": "统一数据集 ID。"},
        },
        "required": ["dataset_id"],
    },
)
async def describe_dataset(dataset_id: str) -> dict:
    from meteora.datasets import get_dataset_catalog

    try:
        dataset = get_dataset_catalog().describe(dataset_id)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    return {"status": "success", "dataset": dataset.to_dict()}


@register_tool(
    name="search_dataset_variables",
    description=(
        "查询统一数据集目录中某个数据集的可下载变量。"
        "对于动态数据目录会实时解析变量，并按数据集时间尺度过滤；"
        "变量不确定或下载失败时应先调用本工具确认；如果内置能力仍不足，可以继续探查源站。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "dataset_id": {"type": "string", "description": "统一数据集 ID。"},
            "query": {"type": "string", "description": "可选变量关键词，如 hgt、air、pressure。"},
        },
        "required": ["dataset_id"],
    },
)
async def search_dataset_variables(dataset_id: str, query: str = "") -> dict:
    from meteora.datasets import get_dataset_catalog

    try:
        variables = await get_dataset_catalog().search_variables(dataset_id, query)
    except (ValueError, OSError, RuntimeError, httpx.HTTPError) as exc:
        return {"status": "error", "message": f"数据集变量查询失败：{exc}"}
    return {
        "status": "success",
        "dataset_id": dataset_id,
        "query": query,
        "count": len(variables),
        "variables": list(variables),
    }


@register_tool(
    name="search_dataset_stations",
    description=(
        "查询站点型数据集的可用观测站。可按站号、站名、ICAO、国家或州搜索，"
        "也可按区域和日期覆盖范围筛选；下载 NOAA ISD 前应先用本工具确认站点。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "dataset_id": {"type": "string", "description": "统一数据集 ID。"},
            "query": {"type": "string", "description": "可选站号、站名、ICAO、国家或州关键词。"},
            "area": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": "可选区域 [north, west, south, east]。",
            },
            "start_date": {"type": "string", "description": "可选开始日期，YYYY-MM-DD。"},
            "end_date": {"type": "string", "description": "可选结束日期，YYYY-MM-DD。"},
        },
        "required": ["dataset_id"],
    },
)
async def search_dataset_stations(
    dataset_id: str,
    query: str = "",
    area: list[float] | None = None,
    start_date: str = "",
    end_date: str = "",
) -> dict:
    from meteora.datasets import get_dataset_catalog

    if area is not None and len(area) != 4:
        return {"status": "error", "message": "area 必须是 [north, west, south, east] 四个数值"}
    try:
        stations = await get_dataset_catalog().search_stations(
            dataset_id,
            query,
            tuple(area) if area is not None else None,
            start_date,
            end_date,
        )
    except (ValueError, OSError, RuntimeError, httpx.HTTPError) as exc:
        return {"status": "error", "message": f"数据集站点查询失败：{exc}"}
    return {
        "status": "success",
        "dataset_id": dataset_id,
        "query": query,
        "count": len(stations),
        "stations": [station.to_dict() for station in stations],
    }


@register_tool(
    name="download_dataset",
    description=(
        "下载由统一数据集目录标记为 download_tool=download_dataset 的数据。"
        "其他数据集应使用查询结果中的 download_tool 路由到对应专用下载能力。"
        "长时间或大体积下载应通过 launch_sub_agent 交给后台执行。"
        "工具会返回远端下载粒度和 warnings；如果 requires_local_subset=true，"
        "必须继续调用 subset_netcdf 做精确时间或空间裁剪，不能直接声称结果已完成裁剪。"
        "NOAA ISD 下载会自动保留原始 CSV，并将可读版常规气象要素 CSV 作为主要结果返回。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "dataset_id": {"type": "string", "description": "统一数据集 ID。"},
            "start_date": {"type": "string", "description": "开始日期，YYYY-MM-DD。"},
            "end_date": {"type": "string", "description": "结束日期，YYYY-MM-DD。"},
            "variables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选变量列表。",
            },
            "levels": {
                "type": "array",
                "items": {"type": "number"},
                "description": "可选垂直层次列表，如气压层 [500, 850]。",
            },
            "stations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选站点列表，可使用规范站号或无歧义的站名、ICAO。",
            },
            "area": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": "可选区域 [north, west, south, east]。",
            },
            "output_dir": {
                "type": "string",
                "description": "可选输出目录；默认写入当前 Meteora 项目的 data 目录。",
            },
        },
        "required": ["dataset_id", "start_date", "end_date"],
    },
)
async def download_dataset(
    dataset_id: str,
    start_date: str,
    end_date: str,
    variables: list[str] | None = None,
    levels: list[float] | None = None,
    stations: list[str] | None = None,
    area: list[float] | None = None,
    output_dir: str | None = None,
) -> dict:
    from meteora.agent.progress import emit_progress
    from meteora.datasets import DatasetDownloadRequest, get_dataset_catalog
    from meteora.toolbox.download_progress import download_progress_reporter

    if area is not None and len(area) != 4:
        return {"status": "error", "message": "area 必须是 [north, west, south, east] 四个数值"}
    destination = Path(output_dir) if output_dir else _default_dataset_output_dir()
    request = DatasetDownloadRequest(
        dataset_id=dataset_id,
        start_date=start_date,
        end_date=end_date,
        output_dir=destination,
        variables=tuple(variables or ()),
        levels=tuple(levels or ()),
        stations=tuple(stations or ()),
        area=tuple(area) if area is not None else None,
    )
    progress_reporter = download_progress_reporter()

    def report_progress(*args: object) -> None:
        if len(args) == 2 and all(isinstance(arg, int | float) for arg in args):
            progress_reporter(int(args[0]), int(args[1]))
            return
        if args:
            emit_progress(str(args[0]))

    try:
        catalog = get_dataset_catalog()
        dataset_name = catalog.describe(dataset_id).name
        emit_progress(f"正在从统一数据目录下载：{dataset_name}")
        result = await catalog.download(request, on_progress=report_progress)
    except ValueError as exc:
        debug_exception("download_dataset invalid request", exc)
        payload = {"status": "error", "message": f"数据集下载失败：{exc}"}
        if dataset_id.startswith("ncep-reanalysis-") and "变量" in str(exc):
            payload.update(
                {
                    "retry_same_request": False,
                    "suggested_tool": "search_dataset_variables",
                    "suggested_args": {
                        "dataset_id": dataset_id,
                        "query": variables[0].split("/")[-1] if variables else "",
                    },
                }
            )
        if dataset_id == "noaa-isd-global-hourly" and any(
            term in str(exc) for term in ("站点", "区域")
        ):
            payload.update(
                {
                    "retry_same_request": False,
                    "suggested_tool": "search_dataset_stations",
                    "suggested_args": {
                        "dataset_id": dataset_id,
                        "query": stations[0] if stations else "",
                        "area": area,
                        "start_date": start_date,
                        "end_date": end_date,
                    },
                }
            )
        return payload
    except (OSError, RuntimeError) as exc:
        debug_exception("download_dataset failed", exc)
        return {"status": "error", "message": f"数据集下载失败：{exc}"}
    except Exception as exc:
        debug_exception("download_dataset unexpected failure", exc)
        return {"status": "error", "message": f"数据集下载遇到远端异常：{exc}"}

    payload = result.to_dict()
    payload["status"] = "success"
    payload["files"] = [short_path(path) for path in result.files]
    payload["reused_files"] = [short_path(path) for path in result.reused_files]
    raw_files = payload.get("metadata", {}).get("raw_files")
    if isinstance(raw_files, list):
        payload["metadata"]["raw_files"] = [short_path(path) for path in raw_files]
    return payload
