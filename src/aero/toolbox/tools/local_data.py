"""Local scientific data discovery, inspection, and registration."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aero.toolbox.paths import find_project_dir, resolve_project_path, short_path
from aero.toolbox.registry import register_tool

_FORMAT_EXTENSIONS = {
    ".nc": "netcdf",
    ".nc4": "netcdf",
    ".cdf": "netcdf",
    ".grib": "grib2",
    ".grb": "grib2",
    ".grib2": "grib2",
    ".csv": "csv",
}
_TIME_NAMES = ("time", "valid_time", "datetime", "date", "timestamp")
_LAT_NAMES = ("latitude", "lat")
_LON_NAMES = ("longitude", "lon")


@register_tool(
    name="scan_local_files",
    description=(
        "递归扫描项目内的本地 NetCDF、GRIB/GRIB2 和 CSV 科研数据文件，"
        "返回变量、时空范围和基础质量检查结果。默认仅预览，不写入记录；"
        "用户确认候选文件后才将可读取文件登记到本地数据记录库。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": "要扫描的项目内目录，默认 data。",
            },
            "pattern": {
                "type": "string",
                "default": "*",
                "description": "可选文件名 glob 过滤，如 '*.nc'。默认扫描支持的所有格式。",
            },
            "confirm": {
                "type": "boolean",
                "default": False,
                "description": "默认 false 仅预览；只有用户确认候选结果后才传 true 写入记录库。",
            },
        },
        "additionalProperties": False,
    },
    requires_confirmation=True,
)
async def scan_local_files(
    directory: str = "data", pattern: str = "*", confirm: bool = False
) -> dict:
    """Discover supported local data files and optionally register readable files."""
    root = find_project_dir().resolve()
    target = resolve_project_path(directory).resolve()
    if not _is_within(target, root):
        return {"status": "error", "message": "只能扫描当前项目目录内的文件。"}
    if not target.exists():
        return {"status": "error", "message": f"目录不存在: {short_path(target)}"}
    if not target.is_dir():
        return {"status": "error", "message": f"不是目录: {short_path(target)}"}

    candidates: list[dict[str, Any]] = []
    for path in sorted(target.rglob(pattern)):
        if not path.is_file() or path.suffix.lower() not in _FORMAT_EXTENSIONS:
            continue
        candidates.append(await _scan_file(path))

    registered: list[dict[str, Any]] = []
    if confirm:
        registered = _register_candidates(candidates)

    counts = {
        "ok": sum(item["status"] == "ok" for item in candidates),
        "warning": sum(item["status"] == "warning" for item in candidates),
        "error": sum(item["status"] == "error" for item in candidates),
    }
    return {
        "status": "success",
        "mode": "confirmed" if confirm else "preview",
        "directory": short_path(target),
        "pattern": pattern,
        "file_count": len(candidates),
        "summary": counts,
        "candidates": candidates,
        "registered": registered,
        "registered_count": len(registered),
        "message": (
            "已登记可读取的本地数据文件。"
            if confirm
            else "扫描结果仅为预览；请向用户展示候选文件并在确认后再登记。"
        ),
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


async def _scan_file(path: Path) -> dict[str, Any]:
    data_format = _FORMAT_EXTENSIONS[path.suffix.lower()]
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        return {
            "path": short_path(path),
            "absolute_path": str(path.resolve()),
            "file_size": None,
            "format": data_format,
            "status": "error",
            "variables": [],
            "dimensions": {},
            "time_range": None,
            "spatial_extent": None,
            "quality": {
                "status": "error",
                "issues": [{"code": "read_error", "message": str(exc)}],
                "variables": {},
            },
            "already_registered": False,
        }
    item: dict[str, Any] = {
        "path": short_path(path),
        "absolute_path": str(path.resolve()),
        "file_size": file_size,
        "format": data_format,
        "status": "ok",
        "variables": [],
        "dimensions": {},
        "time_range": None,
        "spatial_extent": None,
        "quality": {"status": "ok", "issues": [], "variables": {}},
    }
    try:
        if data_format == "netcdf":
            await _scan_netcdf(path, item)
        elif data_format == "grib2":
            await _scan_grib(path, item)
        else:
            await _scan_csv(path, item)
    except Exception as exc:
        item["status"] = "error"
        item["message"] = f"检查失败: {exc}"
        item["quality"] = {
            "status": "error",
            "issues": [{"code": "read_error", "message": str(exc)}],
            "variables": {},
        }
    item["already_registered"] = _is_registered(path)
    return item


async def _scan_netcdf(path: Path, item: dict[str, Any]) -> None:
    from aero.toolbox.tools.netcdf import inspect_nc

    inspected = await inspect_nc(str(path))
    if inspected.get("status") != "ok":
        raise ValueError(inspected.get("message", "无法读取 NetCDF 文件"))
    item["variables"] = list(inspected.get("variables", {}))
    item["dimensions"] = inspected.get("dimensions", {})
    item["time_range"] = inspected.get("time_range")
    if not item["variables"]:
        _issue(item, "no_variables", "文件不包含数据变量")

    import numpy as np
    import xarray as xr

    ds = xr.open_dataset(path)
    try:
        _add_dataset_coordinate_quality(ds, item, np)
        for name, data_array in ds.data_vars.items():
            item["quality"]["variables"][name] = _numeric_quality(data_array.values, np)
    finally:
        ds.close()


async def _scan_grib(path: Path, item: dict[str, Any]) -> None:
    from aero.toolbox.tools.grib import inspect_grib2

    inspected = await inspect_grib2(str(path))
    if inspected.get("status") != "ok":
        raise ValueError(inspected.get("message", "无法读取 GRIB 文件"))
    item["message_count"] = inspected.get("message_count", 0)
    integrity = inspected.get("integrity", {})
    item["integrity"] = integrity
    if not integrity.get("all_messages_end_with_7777", False):
        _issue(item, "incomplete_messages", "存在未以 7777 结束的 GRIB message")
    cfgrib = inspected.get("cfgrib", {})
    if cfgrib.get("available"):
        item["variables"] = list(cfgrib.get("variables", {}))
        item["dimensions"] = cfgrib.get("dimensions", {})
        for name, metadata in cfgrib.get("variables", {}).items():
            item["quality"]["variables"][name] = {
                "dtype": metadata.get("dtype"),
                "missing": None,
                "missing_ratio": None,
                "min": None,
                "max": None,
            }
    else:
        _issue(item, "metadata_limited", "未安装 GRIB 网格解码依赖，仅完成结构完整性检查")


async def _scan_csv(path: Path, item: dict[str, Any]) -> None:
    from aero.toolbox.tools.observations import inspect_csv_table

    inspected = await inspect_csv_table(str(path))
    if inspected.get("status") != "success":
        raise ValueError(inspected.get("message", "无法读取 CSV 文件"))
    item["variables"] = inspected.get("columns", [])
    summary = inspected.get("summary", {})
    if not item["variables"]:
        _issue(item, "no_variables", "CSV 文件不包含字段")
    for name, values in summary.items():
        if values.get("type") == "numeric":
            missing = int(values.get("missing", 0))
            rows = int(inspected.get("rows", 0))
            item["quality"]["variables"][name] = {
                "dtype": "numeric",
                "missing": missing,
                "missing_ratio": missing / rows if rows else 0.0,
                "min": values.get("min"),
                "max": values.get("max"),
            }
    _add_csv_coordinate_quality(path, item)


def _add_dataset_coordinate_quality(ds: Any, item: dict[str, Any], np: Any) -> None:
    time_name = _find_name(ds.coords, _TIME_NAMES)
    if time_name:
        values = ds[time_name].values
        item["time_range"] = _time_quality(values, item, np)
    else:
        _issue(item, "missing_time_coordinate", "未找到时间坐标")
    lat_name = _find_name(ds.coords, _LAT_NAMES)
    lon_name = _find_name(ds.coords, _LON_NAMES)
    if not lat_name or not lon_name:
        _issue(item, "missing_spatial_coordinate", "未同时找到纬度和经度坐标")
        return
    lat = ds[lat_name].values
    lon = ds[lon_name].values
    lat_summary = _coordinate_quality(lat, np)
    lon_summary = _coordinate_quality(lon, np)
    item["spatial_extent"] = {
        "north": lat_summary["max"],
        "south": lat_summary["min"],
        "west": lon_summary["min"],
        "east": lon_summary["max"],
        "latitude_order": lat_summary["order"],
        "longitude_order": lon_summary["order"],
    }
    if lat_summary["missing"] or lon_summary["missing"]:
        _issue(item, "missing_spatial_values", "经纬度坐标存在缺失值")
    if lat_summary["order"] == "mixed" or lon_summary["order"] == "mixed":
        _issue(item, "non_monotonic_spatial_coordinate", "经纬度坐标不是单调序列")


def _add_csv_coordinate_quality(path: Path, item: dict[str, Any]) -> None:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = rows[0].keys() if rows else ()
    time_name = _find_name(fields, _TIME_NAMES)
    lat_name = _find_name(fields, _LAT_NAMES)
    lon_name = _find_name(fields, _LON_NAMES)
    if time_name:
        values = [row.get(time_name, "") for row in rows]
        item["time_range"] = _csv_time_quality(values, item)
    else:
        _issue(item, "missing_time_coordinate", "未找到时间字段")
    if not lat_name or not lon_name:
        _issue(item, "missing_spatial_coordinate", "未同时找到纬度和经度字段")
        return
    lat = _csv_numbers(rows, lat_name)
    lon = _csv_numbers(rows, lon_name)
    if not lat or not lon:
        _issue(item, "invalid_spatial_coordinate", "经纬度字段不包含有效数值")
        return
    item["spatial_extent"] = {
        "north": max(lat),
        "south": min(lat),
        "west": min(lon),
        "east": max(lon),
        "latitude_order": _order(lat),
        "longitude_order": _order(lon),
    }


def _find_name(names: Any, candidates: tuple[str, ...]) -> str | None:
    lookup = {str(name).casefold(): str(name) for name in names}
    return next((lookup[name] for name in candidates if name in lookup), None)


def _numeric_quality(values: Any, np: Any) -> dict[str, Any]:
    array = np.asarray(values)
    if not np.issubdtype(array.dtype, np.number):
        return {
            "dtype": str(array.dtype),
            "missing": None,
            "missing_ratio": None,
            "min": None,
            "max": None,
        }
    finite = np.isfinite(array)
    count = int(array.size)
    missing = count - int(finite.sum())
    valid = array[finite]
    return {
        "dtype": str(array.dtype),
        "missing": missing,
        "missing_ratio": missing / count if count else 0.0,
        "min": _json_number(valid.min()) if valid.size else None,
        "max": _json_number(valid.max()) if valid.size else None,
    }


def _coordinate_quality(values: Any, np: Any) -> dict[str, Any]:
    array = np.asarray(values).reshape(-1)
    finite = np.isfinite(array)
    valid = array[finite]
    return {
        "missing": int(array.size - finite.sum()),
        "min": _json_number(valid.min()) if valid.size else None,
        "max": _json_number(valid.max()) if valid.size else None,
        "order": _order(valid.tolist()),
    }


def _time_quality(values: Any, item: dict[str, Any], np: Any) -> dict[str, Any]:
    array = np.asarray(values).reshape(-1)
    text = [str(value) for value in array]
    duplicate_count = len(text) - len(set(text))
    if duplicate_count:
        _issue(item, "duplicate_time", "时间坐标包含重复值", count=duplicate_count)
    intervals: list[int] = []
    try:
        numeric = array.astype("datetime64[ns]").astype("int64")
        intervals = [int(value) for value in np.diff(numeric)]
    except (TypeError, ValueError):
        _issue(item, "unreadable_time", "时间坐标无法标准化比较")
    regular = len(set(intervals)) <= 1 if intervals else True
    if intervals and not regular:
        _issue(item, "irregular_time_interval", "时间坐标间隔不一致")
    return {
        "start": text[0] if text else None,
        "end": text[-1] if text else None,
        "count": len(text),
        "duplicate_count": duplicate_count,
        "regular_interval": regular,
    }


def _csv_time_quality(values: list[str], item: dict[str, Any]) -> dict[str, Any]:
    cleaned = [value.strip() for value in values if value and value.strip()]
    duplicate_count = len(cleaned) - len(set(cleaned))
    if duplicate_count:
        _issue(item, "duplicate_time", "时间字段包含重复值", count=duplicate_count)
    parsed: list[datetime] = []
    for value in cleaned:
        try:
            parsed.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            _issue(item, "unreadable_time", "时间字段无法标准化比较")
            return {
                "start": cleaned[0] if cleaned else None,
                "end": cleaned[-1] if cleaned else None,
                "count": len(cleaned),
                "duplicate_count": duplicate_count,
                "regular_interval": None,
            }
    intervals = [int((right - left).total_seconds()) for left, right in zip(parsed, parsed[1:])]
    regular = len(set(intervals)) <= 1 if intervals else True
    if intervals and not regular:
        _issue(item, "irregular_time_interval", "时间字段间隔不一致")
    return {
        "start": cleaned[0] if cleaned else None,
        "end": cleaned[-1] if cleaned else None,
        "count": len(cleaned),
        "duplicate_count": duplicate_count,
        "regular_interval": regular,
    }


def _csv_numbers(rows: list[dict[str, str]], name: str) -> list[float]:
    numbers = []
    for row in rows:
        try:
            numbers.append(float(row.get(name, "")))
        except (TypeError, ValueError):
            continue
    return numbers


def _order(values: list[float]) -> str:
    if len(values) < 2:
        return "single"
    deltas = [right - left for left, right in zip(values, values[1:])]
    if all(delta >= 0 for delta in deltas):
        return "ascending"
    if all(delta <= 0 for delta in deltas):
        return "descending"
    return "mixed"


def _json_number(value: Any) -> float:
    return float(value)


def _issue(item: dict[str, Any], code: str, message: str, **details: Any) -> None:
    issue = {"code": code, "message": message, **details}
    item["quality"]["issues"].append(issue)
    if item["quality"]["status"] != "error":
        item["quality"]["status"] = "warning"
    if item["status"] != "error":
        item["status"] = "warning"


def _is_registered(path: Path) -> bool:
    from aero.data.download_store import CDSDownloadStore

    database = find_project_dir() / "aero_downloads.db"
    if not database.exists():
        return False
    store = CDSDownloadStore(database)
    return store.get_by_file_path(str(path.resolve())) is not None


def _register_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from aero.data.download_store import CDSDownloadStore

    store = CDSDownloadStore(find_project_dir() / "aero_downloads.db")
    registered = []
    for candidate in candidates:
        if candidate["status"] == "error" or candidate["already_registered"]:
            continue
        path = candidate["absolute_path"]
        if store.get_by_file_path(path):
            continue
        record_id = store.insert(
            source="local",
            dataset_id=f"local-{candidate['format']}",
            variables=candidate["variables"],
            data_format=candidate["format"],
            file_path=path,
            file_size=candidate["file_size"],
            total_bytes=candidate["file_size"],
            downloaded_bytes=candidate["file_size"],
            status="confirmed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            notes=json.dumps(
                {
                    "scan": {
                        "dimensions": candidate["dimensions"],
                        "time_range": candidate["time_range"],
                        "spatial_extent": candidate["spatial_extent"],
                        "quality": candidate["quality"],
                    }
                },
                ensure_ascii=False,
            ),
        )
        registered.append({"download_id": record_id, "path": candidate["path"]})
    return registered
