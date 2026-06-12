"""GRIB2 inspection tools and lightweight structural parser."""

from __future__ import annotations

import json
from pathlib import Path

from aero.toolbox.paths import find_project_dir, short_path
from aero.toolbox.registry import register_tool


def _parse_grib2_messages(data: bytes) -> list[dict]:
    messages = []
    offset = 0
    index = 1
    while offset < len(data):
        next_grib = data.find(b"GRIB", offset)
        if next_grib < 0:
            break
        if len(data) - next_grib < 16:
            break

        edition = data[next_grib + 7]
        total_length = int.from_bytes(data[next_grib + 8 : next_grib + 16], "big")
        if edition != 2 or total_length < 16:
            offset = next_grib + 4
            continue

        end = next_grib + total_length
        if end > len(data):
            messages.append(
                {
                    "index": index,
                    "offset": next_grib,
                    "byte_length": total_length,
                    "edition": edition,
                    "discipline": data[next_grib + 6],
                    "complete": False,
                    "ends_with_7777": False,
                    "error": "message length exceeds file size",
                }
            )
            break

        metadata = _parse_grib2_product_metadata(data[next_grib:end], next_grib)
        messages.append(
            {
                "index": index,
                "offset": next_grib,
                "byte_length": total_length,
                "range": f"bytes={next_grib}-{end - 1}",
                "edition": edition,
                "discipline": data[next_grib + 6],
                "complete": True,
                "ends_with_7777": data[end - 4 : end] == b"7777",
                **metadata,
            }
        )
        index += 1
        offset = end
    return messages


def _parse_grib2_product_metadata(message: bytes, message_offset: int) -> dict:
    pos = 16
    metadata: dict = {}
    sections = []
    while pos + 5 <= len(message) - 4:
        section_length = int.from_bytes(message[pos : pos + 4], "big")
        section_number = message[pos + 4]
        if section_length < 5 or pos + section_length > len(message):
            sections.append(
                {
                    "number": section_number,
                    "offset": message_offset + pos,
                    "error": "invalid section length",
                }
            )
            break
        sections.append(
            {
                "number": section_number,
                "offset": message_offset + pos,
                "byte_length": section_length,
            }
        )
        if section_number == 4 and section_length >= 11:
            metadata["product_definition_template"] = int.from_bytes(
                message[pos + 7 : pos + 9],
                "big",
            )
            metadata["category"] = message[pos + 9]
            metadata["parameter_number"] = message[pos + 10]
        pos += section_length
    metadata["sections"] = sections
    return metadata


async def _load_gfs_parameter_lookup() -> dict[tuple[int, int, int], dict]:
    try:
        from aero.data.gfs_params import get_gfs_parameters

        parameters = await get_gfs_parameters()
    except Exception:
        return {}

    lookup = {}
    for item in parameters:
        lookup[(item["discipline"], item["category"], item["number"])] = {
            "abbrev": item["abbrev"],
            "name": item["parameter"],
            "units": item["units"],
            "source_url": item["source_url"],
        }
    return lookup


def _lookup_download_record_for_file(path: Path) -> dict | None:
    try:
        from aero.data.download_store import CDSDownloadStore

        project_dir = find_project_dir()
        store = CDSDownloadStore(project_dir / "aero_downloads.db")
        record = store.get_by_file_path(str(path))
        if not record:
            record = store.get_by_file_path(str(path.absolute()))
        if not record:
            return None
        notes = record.get("notes")
        if isinstance(notes, str):
            try:
                notes = json.loads(notes)
            except json.JSONDecodeError:
                pass
        return {
            "download_id": record.get("id"),
            "source": record.get("source"),
            "dataset_id": record.get("dataset_id"),
            "variables": record.get("variables"),
            "download_url": record.get("download_url"),
            "notes": notes,
        }
    except Exception:
        return None


def _inspect_grib2_with_cfgrib(path: Path) -> dict:
    try:
        import xarray as xr

        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    except Exception as e:
        return {
            "available": False,
            "message": ("未使用 cfgrib 解码。安装 cfgrib/eccodes 后可查看网格维度和坐标。"),
            "error": str(e),
        }

    try:
        variables = {}
        for vname in ds.data_vars:
            da = ds[vname]
            variables[vname] = {
                "dims": list(da.dims),
                "shape": list(da.shape),
                "dtype": str(da.dtype),
                "units": da.attrs.get("units"),
                "long_name": da.attrs.get("long_name"),
            }
        return {
            "available": True,
            "variables": variables,
            "dimensions": {name: size for name, size in ds.sizes.items()},
            "coords": list(ds.coords),
        }
    finally:
        ds.close()




@register_tool(
    name="inspect_grib2",
    description=(
        "检查本地 GRIB2 文件，返回消息数量、字节范围、GRIB 参数编号、"
        "可识别的要素含义，以及可选的 cfgrib/xarray 维度信息。"
        "用于查看 GFS 等 GRIB2 下载结果是否正常，不重新下载。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "GRIB2 文件的完整路径",
            },
        },
        "required": ["file_path"],
    },
)
async def inspect_grib2(file_path: str) -> dict:
    """Inspect a local GRIB2 file and return structural metadata."""
    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"文件不存在: {short_path(file_path)}"}
    if not path.is_file():
        return {"status": "error", "message": f"不是文件: {short_path(file_path)}"}

    try:
        data = path.read_bytes()
    except Exception as e:
        return {"status": "error", "message": f"无法读取文件: {e}"}

    messages = _parse_grib2_messages(data)
    if not messages:
        return {
            "status": "error",
            "message": "未识别到 GRIB2 message。请确认文件是否为 GRIB2 格式。",
            "file": short_path(path),
            "file_size": path.stat().st_size,
        }

    parameter_lookup = await _load_gfs_parameter_lookup()
    for message in messages:
        key = (
            message.get("discipline"),
            message.get("category"),
            message.get("parameter_number"),
        )
        if key in parameter_lookup:
            message["parameter"] = parameter_lookup[key]

    info = {
        "file": short_path(path),
        "file_size": path.stat().st_size,
        "status": "ok",
        "format": "grib2",
        "message_count": len(messages),
        "messages": messages,
        "integrity": {
            "starts_with_grib": data.startswith(b"GRIB"),
            "all_messages_end_with_7777": all(m["ends_with_7777"] for m in messages),
            "parsed_bytes": sum(m["byte_length"] for m in messages),
            "trailing_bytes": len(data) - sum(m["byte_length"] for m in messages),
        },
    }
    info["download_record"] = _lookup_download_record_for_file(path)
    info["cfgrib"] = _inspect_grib2_with_cfgrib(path)
    return info


