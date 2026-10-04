"""ERA5 CDS availability and download tools."""

from __future__ import annotations

import asyncio
from calendar import monthrange
from pathlib import Path

import structlog

from aero.toolbox.config_access import find_config
from aero.toolbox.download_progress import download_progress_reporter
from aero.toolbox.paths import find_project_dir, short_path
from aero.toolbox.registry import register_tool
from aero.toolbox.secret_input import credential_request_for

logger = structlog.get_logger()


@register_tool(
    name="download_era5",
    description=(
        "通过 CDS（Copernicus Climate Data Store）下载 ERA5 再分析格点数据。\n\n"
        "先调用 search_cds_variables 确认变量名和层级类型，根据结果确定传什么参数。\n\n"
        "常用 dataset_id:\n"
        "  reanalysis-era5-pressure-levels          高空逐小时\n"
        "  reanalysis-era5-single-levels           地表逐小时\n"
        "  reanalysis-era5-pressure-levels-monthly-means  高空月均值\n"
        "  reanalysis-era5-single-levels-monthly-means   地表月均值\n\n"
        "同一天、同区域、同一变量组的多个气压层必须优先通过 pressure_levels 合并为一次请求，"
        "不要逐层提交。地表变量属于不同数据集，需要单独请求。"
        "不传 dataset_id 则根据 pressure_levels/pressure_level 自动选择默认逐小时数据集。"
        "月均值数据集不需要传 day。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "variables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "变量列表，如 ['u', 'v', 'z', 't'] 或 ['total_precipitation']",
            },
            "year": {
                "type": "integer",
                "description": "年份",
            },
            "month": {
                "type": "integer",
                "description": "月份 (1-12)",
            },
            "day": {
                "type": "integer",
                "description": (
                    "日期 (1-31)。用户指定某一天时必须填写，如 2025年7月8日填 8；不填则下载整月"
                ),
            },
            "dataset_id": {
                "type": "string",
                "description": (
                    "数据集 ID。不填则根据 pressure_level 自动选择默认 ERA5 逐小时数据集。"
                    "月均值传 reanalysis-era5-*-monthly-means 等"
                ),
            },
            "pressure_level": {
                "type": "integer",
                "description": "兼容旧调用的单个气压层 hPa，如 500；多层必须改用 pressure_levels",
            },
            "pressure_levels": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
                "uniqueItems": True,
                "description": (
                    "多个气压层 hPa，如 [1000, 925, 850, 700, 600, 500, 400]。"
                    "同一请求需要多层时必须一次传入此列表，不要逐层调用"
                ),
            },
            "area": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": "[north, west, south, east]，如 [60, 100, 20, 150] 表示东亚",
            },
            "data_format": {
                "type": "string",
                "enum": ["netcdf", "grib"],
                "description": "输出格式，默认 netcdf",
            },
            "time": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "逐小时数据的时刻列表，如 ['00:00', '06:00', '12:00', '18:00']。"
                    "不填默认下载全天 24 个小时。月均值数据不需要此参数"
                ),
            },
        },
        "required": ["variables", "year", "month"],
        "additionalProperties": False,
    },
)
async def download_era5(
    variables: list[str],
    year: int,
    month: int,
    day: int | None = None,
    dataset_id: str | None = None,
    pressure_level: int | None = None,
    pressure_levels: list[int] | None = None,
    area: list[float] | None = None,
    data_format: str = "netcdf",
    time: list[str] | None = None,
) -> dict:
    """Download ERA5 data from CDS."""
    from threading import Lock

    from aero.agent.progress import cancel_requested as _cancelled
    from aero.agent.progress import emit_progress
    from aero.data.download_store import CDSDownloadStore

    try:
        year = int(year)
    except (TypeError, ValueError):
        return {"status": "error", "message": f"year 必须是整数: {year}"}
    try:
        month = int(month)
    except (TypeError, ValueError):
        return {"status": "error", "message": f"month 必须是整数: {month}"}
    if day is not None:
        try:
            day = int(day)
        except (TypeError, ValueError):
            return {"status": "error", "message": f"day 必须是整数: {day}"}

    if day is not None and not 1 <= day <= monthrange(year, month)[1]:
        return {
            "status": "error",
            "message": f"日期无效：{year}-{month:02d}-{day:02d}",
        }
    try:
        requested_pressure_levels = _normalize_pressure_levels(
            pressure_level=pressure_level,
            pressure_levels=pressure_levels,
        )
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    pressure_level_value = requested_pressure_levels or None

    project_dir = find_project_dir()
    store = CDSDownloadStore(project_dir / "aero_downloads.db")

    if dataset_id is None:
        if requested_pressure_levels:
            dataset_id = "reanalysis-era5-pressure-levels"
        else:
            dataset_id = "reanalysis-era5-single-levels"

    cached_result = _reuse_existing_era5_file(
        store=store,
        source="cds",
        dataset_id=dataset_id,
        variables=variables,
        year=year,
        month=month,
        day=day,
        pressure_level=pressure_level_value,
        area=area,
        data_format=data_format,
        time=time,
    )
    if cached_result is not None:
        return cached_result

    emit_progress("准备下载数据")
    from aero.adapters.cds_adapter import CDSAdapter

    config = find_config()
    cds_cfg = config.credentials.cds

    if not cds_cfg.key:
        return {
            "status": "error",
            "setup_required": "cds",
            "credential_request": credential_request_for("cds"),
            "message": (
                "CDS API key 未配置。请在 https://cds.climate.copernicus.eu/ 注册账户，"
                "进入 User Profile → API key 并复制官方显示的两行配置。"
                "先向用户说明这些步骤，并要求用户只回复“准备好了”或“打开安全输入框”；"
                "不得要求其将凭证发送到聊天框，也不得立刻弹出输入窗口。"
            ),
        }

    adapter = CDSAdapter(cds_url=cds_cfg.url, cds_key=cds_cfg.key)

    _row_id: int = 0
    _row_lock = Lock()

    def _save_request_id(request_id: str, dest_path: Path) -> None:
        nonlocal _row_id
        if not request_id or request_id == "unknown":
            return
        with _row_lock:
            if _row_id:
                store.update_by_id(_row_id, request_id=request_id, status="submitted")
                return
            _row_id = store.insert(
                request_id=request_id,
                dataset_id=dataset_id,
                variables=variables,
                year=year,
                month=month,
                day=day,
                pressure_level=pressure_level_value,
                area=area,
                data_format=data_format,
                file_path=str(dest_path),
                status="queued",
            )

    def _save(request_id: str, download_url: str, dest_path: Path) -> None:
        nonlocal _row_id
        with _row_lock:
            if _row_id:
                store.update_by_id(
                    _row_id,
                    request_id=request_id,
                    download_url=download_url,
                    file_path=str(dest_path),
                    status="downloading",
                )
                return
            _row_id = store.insert(
                request_id=request_id,
                dataset_id=dataset_id,
                variables=variables,
                year=year,
                month=month,
                day=day,
                pressure_level=pressure_level_value,
                area=area,
                data_format=data_format,
                file_path=str(dest_path),
                download_url=download_url,
                status="downloading",
            )

    # ``request_overrides`` replaces (rather than augments) the adapter's
    # default request.  A custom time therefore must carry the remaining ERA5
    # request fields too.  Passing only {"time": ...} used to drop ``day``
    # and the required product/format fields, which CDS misleadingly reports
    # as "data not available yet" even for historical dates.
    overrides = _era5_time_request_overrides(
        dataset_id=dataset_id,
        day=day,
        data_format=data_format,
        time=time,
    )

    # ── CDS submit (retry transient failures only, up to 5 times) ──
    cds_submit_error = None
    meta = None
    for cds_attempt in range(5):
        try:
            meta = await adapter.submit(
                dataset_id=dataset_id,
                variables=variables,
                year=year,
                month=month,
                day=day,
                pressure_level=pressure_level_value,
                area=area,
                data_format=data_format,
                request_overrides=overrides,
                on_submitted=_save,
                on_request_id=_save_request_id,
            )
            break
        except BaseException as e:
            cds_submit_error = e
            if isinstance(e, asyncio.CancelledError):
                break
            if _is_non_retryable_cds_submit_error(e):
                emit_progress("CDS 拒绝了该请求（参数或可用时次无效），不会重复提交。")
                break
            if cds_attempt < 4:
                delay = 2**cds_attempt
                emit_progress(f"CDS 提交失败，{delay}s 后重试（第 {cds_attempt + 1}/5 次）: {e}")
                await asyncio.sleep(delay)
    if meta is None:
        err_msg = (
            str(cds_submit_error)
            if not isinstance(cds_submit_error, asyncio.CancelledError)
            else "用户中断"
        )
        with _row_lock:
            if _row_id == 0:
                _row_id = store.insert(
                    dataset_id=dataset_id,
                    variables=variables,
                    year=year,
                    month=month,
                    day=day,
                    pressure_level=pressure_level_value,
                    area=area,
                    data_format=data_format,
                    file_path=str(project_dir / config.output.data_dir),
                    status="error",
                    error_msg=err_msg,
                )
            else:
                status = (
                    "download_failed"
                    if isinstance(cds_submit_error, asyncio.CancelledError)
                    else "error"
                )
                store.update_by_id(_row_id, status=status, error_msg=err_msg)
        if isinstance(cds_submit_error, asyncio.CancelledError):
            raise
        return {"status": "error", "message": f"CDS 提交失败: {err_msg}", "download_id": _row_id}

    download_url = meta["download_url"]
    dest_path = meta["dest_path"]
    total_bytes = meta["total_bytes"]

    if total_bytes > 0:
        store.update_by_id(_row_id, total_bytes=total_bytes)

    if _cancelled():
        store.update_by_id(_row_id, status="download_failed", error_msg="用户中断")
        return {
            "status": "cancelled",
            "message": "下载已取消。CDS 任务已提交，request_id 已保存，稍后可以继续下载。",
            "download_id": _row_id,
            "request_id": meta["request_id"],
            "download_url": download_url,
        }

    # ── CDS fetch (retry up to 5 times) ──
    emit_progress(f"正在下载文件：{short_path(dest_path)}")
    progress_reporter = download_progress_reporter()
    cds_fetch_error = None
    for cds_fetch_attempt in range(5):
        try:
            # Each HTTP connection needs its own throughput baseline while
            # retaining one progress identity for the UI.  On a resumed
            # attempt, bytes already on disk were not transferred through the
            # new connection.
            if total_bytes > 0:
                progress_reporter(
                    min(dest_path.stat().st_size, total_bytes) if dest_path.exists() else 0,
                    total_bytes,
                    force=True,
                    reset_measurement=cds_fetch_attempt > 0,
                )
            file_size = await adapter.fetch(
                download_url=download_url,
                dest_path=dest_path,
                on_progress=progress_reporter,
                total_bytes=total_bytes,
            )
            break
        except BaseException as e:
            cds_fetch_error = e
            if isinstance(e, asyncio.CancelledError):
                break
            if cds_fetch_attempt < 4:
                delay = 2**cds_fetch_attempt
                emit_progress(
                    f"CDS 文件下载失败，{delay}s 后重试（第 {cds_fetch_attempt + 1}/5 次）: {e}"
                )
                await asyncio.sleep(delay)
    else:
        err_msg = (
            str(cds_fetch_error)
            if not isinstance(cds_fetch_error, asyncio.CancelledError)
            else "用户中断"
        )
        store.update_by_id(_row_id, status="download_failed", error_msg=err_msg)
        if isinstance(cds_fetch_error, asyncio.CancelledError):
            raise
        return {
            "status": "error",
            "message": f"数据下载失败：{err_msg}",
            "download_id": _row_id,
            "request_id": meta["request_id"],
            "download_url": download_url,
        }

    store.update_by_id(_row_id, status="completed_with_file", file_size=file_size)

    info = {}
    try:
        import xarray as xr

        engine = "cfgrib" if data_format == "grib" else None
        ds = xr.open_dataset(dest_path, engine=engine)
        info = _summarize_dataset(ds, variables)
    except Exception as e:
        logger.warning("xarray.open_failed", path=str(dest_path), error=str(e))

    format_note = (
        "GRIB2 格式，可使用 cfgrib 或 eccodes 处理。"
        if data_format == "grib"
        else "NetCDF4 文件可能使用 HDF5 作为底层容器，但数据格式仍是 NetCDF。"
    )

    return {
        "status": "success",
        "download_id": _row_id,
        "request_id": meta["request_id"],
        "file_path": short_path(dest_path),
        "variables": variables,
        "pressure_levels": requested_pressure_levels,
        "data_source": "cds",
        "time_range": {"year": year, "month": f"{month:02d}", "day": day},
        "region": {"north": area[0], "west": area[1], "south": area[2], "east": area[3]}
        if area
        else None,
        "format": {
            "requested": data_format,
            "download_format": "unarchived",
            "actual": "netcdf4/hdf5" if dest_path.suffix == ".nc" else "grib",
            "note": format_note,
        },
        "summary": info,
        "references": [
            "https://cds.climate.copernicus.eu/",
            f"https://cds.climate.copernicus.eu/datasets/{dataset_id}",
        ],
    }


@register_tool(
    name="check_era5_availability",
    description=(
        "查询 ERA5 CDS 数据源的可用性、时间范围和探测 URL。"
        "当用户问 ERA5 是否可用、某年月是否可下载时调用。"
        "这是只读检查，不下载数据。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "variables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "变量列表，如 ['2m_temperature'] 或 ['total_precipitation']",
            },
            "source": {
                "type": "string",
                "enum": ["cds"],
                "default": "cds",
                "description": "要检查的数据源。",
            },
            "year": {
                "type": "integer",
                "description": "可选年份；提供 year+month 时检查该年月的具体可用性。",
            },
            "month": {
                "type": "integer",
                "description": "可选月份；提供 year+month 时检查该年月的具体可用性。",
            },
            "day": {
                "type": "integer",
                "description": "可选日期；用于检查单日请求对应的源文件。",
            },
            "pressure_level": {
                "type": "integer",
                "description": "可选气压层 hPa；不填表示地表/单层变量。",
            },
            "refresh": {
                "type": "boolean",
                "default": False,
                "description": "是否忽略本地缓存并重新访问远程元数据。",
            },
        },
        "required": ["variables"],
        "additionalProperties": False,
    },
)
async def check_era5_availability(
    variables: list[str],
    source: str = "cds",
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
    pressure_level: int | None = None,
    refresh: bool = False,
) -> dict:
    """Check ERA5 source availability without downloading data."""
    from aero.data.era5_availability import check_era5_source_availability

    if bool(year is None) ^ bool(month is None):
        return {
            "status": "error",
            "message": "year 和 month 必须同时提供，或同时不提供。",
        }
    if year is not None:
        try:
            year = int(year)
            month = int(month) if month is not None else None
        except (TypeError, ValueError):
            return {"status": "error", "message": "year/month 必须是整数。"}
        if month is None or not 1 <= month <= 12:
            return {"status": "error", "message": f"month 必须是 1-12：{month}"}
        if day is not None:
            try:
                day = int(day)
            except (TypeError, ValueError):
                return {"status": "error", "message": "day 必须是整数。"}
            if not 1 <= day <= monthrange(year, month)[1]:
                return {"status": "error", "message": f"日期无效：{year}-{month:02d}-{day:02d}"}

    return await check_era5_source_availability(
        variables=variables,
        year=year,
        month=month,
        day=day,
        pressure_level=pressure_level,
        source=source,
        refresh=refresh,
    )


def _era5_time_request_overrides(
    *,
    dataset_id: str,
    day: int | None,
    data_format: str,
    time: list[str] | None,
) -> dict | None:
    """Build a complete ERA5 request only when callers select hourly times.

    ``CDSAdapter.request_overrides`` intentionally replaces its normal request
    body so that ADS/CAMS can supply their own schema.  ERA5's custom-time
    case must consequently include the normal ERA5 fields explicitly.
    """
    if not time:
        return None

    request = {
        "product_type": ["reanalysis"],
        "data_format": data_format,
        "download_format": "unarchived",
        "time": time,
    }
    if "monthly" not in dataset_id:
        request["day"] = [f"{day:02d}"] if day else [f"{d:02d}" for d in range(1, 32)]
    return request


def _normalize_pressure_levels(
    *,
    pressure_level: int | None,
    pressure_levels: list[int] | None,
) -> list[int]:
    if pressure_level is not None and pressure_levels:
        raise ValueError(
            "pressure_level 和 pressure_levels 不能同时传；多层请只用 pressure_levels。"
        )
    raw_levels = (
        pressure_levels
        if pressure_levels
        else ([pressure_level] if pressure_level is not None else [])
    )
    normalized: list[int] = []
    for raw in raw_levels:
        try:
            level = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"气压层必须是整数 hPa：{raw}") from exc
        if not 1 <= level <= 1000:
            raise ValueError(f"气压层超出 ERA5 标准范围 1-1000 hPa：{level}")
        if level not in normalized:
            normalized.append(level)
    return normalized


def _is_non_retryable_cds_submit_error(error: BaseException) -> bool:
    """Return whether retrying a CDS submit cannot change the outcome."""
    text = str(error).lower()
    return (
        "400 client error" in text
        or "invalid request" in text
        or "none of the data you have requested is available yet" in text
    )


def _reuse_existing_era5_file(
    *,
    store,
    source: str,
    dataset_id: str,
    variables: list[str],
    year: int,
    month: int,
    day: int | None,
    pressure_level: int | list[int] | None,
    area: list[float] | None,
    data_format: str,
    time: list[str] | None,
) -> dict | None:
    if data_format not in ("netcdf", "grib"):
        return None

    from aero.adapters.cds_adapter import CDSAdapter

    candidates: list[tuple[str, Path, str]] = []
    candidates.append(
        (
            "cds",
            CDSAdapter._build_dest_path(
                dataset_id,
                variables,
                year,
                month,
                day,
                pressure_level,
                data_format,
                area=area,
                time=time,
            ),
            "cds",
        )
    )

    found: tuple[str, Path, str] | None = None
    for candidate in candidates:
        if candidate[1].exists() and candidate[1].stat().st_size > 0:
            found = candidate
            break
    if found is None:
        return None

    data_source, dest_path, record_source = found

    info = {}
    try:
        import xarray as xr

        engine = "cfgrib" if data_format == "grib" else None
        ds = xr.open_dataset(dest_path, engine=engine)
        info = _summarize_dataset(ds, variables)
        ds.close()
    except Exception as e:
        logger.warning("era5.local_cache_unreadable", path=str(dest_path), error=str(e))
        return None

    record = store.get_by_file_path(str(dest_path))
    download_id = record.get("id") if record else None
    request_id = record.get("request_id") if record else None
    file_size = dest_path.stat().st_size

    return {
        "status": "success",
        "download_id": download_id,
        "request_id": request_id,
        "file_path": short_path(dest_path),
        "variables": variables,
        "data_source": data_source,
        "cached": True,
        "message": "本地已有完整文件，已直接复用，未重复下载。",
        "time_range": {"year": year, "month": f"{month:02d}", "day": day},
        "region": {"north": area[0], "west": area[1], "south": area[2], "east": area[3]}
        if area
        else None,
        "format": {
            "requested": data_format,
            "download_format": "unarchived",
            "actual": "netcdf4/hdf5" if dest_path.suffix == ".nc" else "grib",
            "note": "复用本地已完成文件。",
        },
        "summary": info,
        "file_size": file_size,
        "references": [
            "https://cds.climate.copernicus.eu/",
            f"https://cds.climate.copernicus.eu/datasets/{dataset_id}",
        ]
        if record_source == "cds"
        else [],
    }


def _summarize_dataset(ds, variables: list[str]) -> dict:
    info = {}
    for v in variables:
        if v in ds:
            da = ds[v]
            info[v] = {
                "shape": list(da.shape),
                "dims": list(da.dims),
                "dtype": str(da.dtype),
            }
    return info
