"""NetCDF inspection and deterministic subsetting tools."""

from pathlib import Path

from meteora.toolbox.paths import short_path
from meteora.toolbox.registry import register_tool


@register_tool(
    name="inspect_nc",
    description=(
        "检查本地 NetCDF 文件，返回变量、维度、时间范围、形状等元数据。"
        "用于查看已下载的文件是否正常，不重新下载。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "NetCDF 文件的完整路径",
            },
        },
        "required": ["file_path"],
    },
)
async def inspect_nc(file_path: str) -> dict:
    """Inspect a local NetCDF file and return its metadata."""
    import xarray as xr

    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"文件不存在: {short_path(file_path)}"}

    try:
        ds = xr.open_dataset(path)
    except Exception as e:
        return {"status": "error", "message": f"无法打开文件: {e}"}

    info = {
        "file": short_path(path),
        "file_size": path.stat().st_size,
        "status": "ok",
        "variables": {},
        "dimensions": {name: size for name, size in ds.sizes.items()},
        "coords": list(ds.coords),
    }

    for vname in ds.data_vars:
        da = ds[vname]
        info["variables"][vname] = {
            "dims": list(da.dims),
            "shape": list(da.shape),
            "dtype": str(da.dtype),
        }
        if "units" in da.attrs:
            info["variables"][vname]["units"] = da.attrs["units"]
        if "long_name" in da.attrs:
            info["variables"][vname]["long_name"] = da.attrs["long_name"]

    if "time" in ds.coords:
        t = ds.coords["time"]
        info["time_range"] = {
            "start": str(t.values[0]),
            "end": str(t.values[-1]),
            "count": len(t),
        }

    ds.close()
    return info


@register_tool(
    name="subset_netcdf",
    description=(
        "裁剪本地 NetCDF 文件的时间、空间和变量，输出新的 NetCDF 文件。"
        "用于把 GCS/AWS 先下载的整月 ERA5 文件裁成某一天或某个区域，也可用于普通 NetCDF 子集提取。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "input_path": {
                "type": "string",
                "description": "输入 NetCDF 文件路径",
            },
            "output_path": {
                "type": "string",
                "description": "输出 NetCDF 文件路径。不填则自动在同目录生成 *_subset.nc",
            },
            "start_time": {
                "type": "string",
                "description": "开始时间，如 2019-02-14T00:00。不填则不裁剪时间下限",
            },
            "end_time": {
                "type": "string",
                "description": "结束时间，如 2019-02-14T23:00。不填则不裁剪时间上限",
            },
            "area": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": "[north, west, south, east]。不填则不裁剪空间",
            },
            "variables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要保留的变量名列表。不填保留所有变量",
            },
            "levels": {
                "type": "array",
                "items": {"type": "number"},
                "description": "要保留的垂直层次，如 [500, 850]。不填则保留所有层次",
            },
            "overwrite": {
                "type": "boolean",
                "default": False,
                "description": "输出文件已存在时是否覆盖",
            },
        },
        "required": ["input_path"],
        "additionalProperties": False,
    },
)
async def subset_netcdf(
    input_path: str,
    output_path: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    area: list[float] | None = None,
    variables: list[str] | None = None,
    levels: list[float] | None = None,
    overwrite: bool = False,
) -> dict:
    """Subset a local NetCDF file by time, area, and variables."""
    try:
        result = _subset_netcdf_file(
            input_path=Path(input_path),
            output_path=Path(output_path) if output_path else None,
            start_time=start_time,
            end_time=end_time,
            area=area,
            variables=variables,
            levels=levels,
            overwrite=overwrite,
        )
    except Exception as e:
        return {"status": "error", "message": f"裁剪失败: {e}"}

    return result


def _subset_netcdf_file(
    *,
    input_path: Path,
    output_path: Path | None,
    start_time: str | None,
    end_time: str | None,
    area: list[float] | None,
    variables: list[str] | None,
    levels: list[float] | None,
    overwrite: bool,
) -> dict:
    import xarray as xr

    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"文件不存在: {short_path(input_path)}")
    if output_path is None:
        output_path = input_path.with_name(f"{input_path.stem}_subset{input_path.suffix}")
    output_path = Path(output_path)
    if output_path.resolve() == input_path.resolve():
        raise ValueError("输出文件不能覆盖输入文件，请指定新的 output_path")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"输出文件已存在: {short_path(output_path)}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ds = xr.open_dataset(input_path)
    try:
        original_sizes = {name: int(size) for name, size in ds.sizes.items()}

        if variables:
            missing = [v for v in variables if v not in ds.data_vars]
            if missing:
                raise KeyError(f"变量不存在: {missing}")
            ds = ds[variables]

        time_coord = _find_coord_name(ds, ("time", "valid_time"))
        if time_coord and (start_time or end_time):
            start = start_time if start_time else None
            end = end_time if end_time else None
            ds = ds.sel({time_coord: slice(start, end)})
        elif start_time or end_time:
            raise ValueError("文件中未找到 time/valid_time 坐标，无法裁剪时间")

        if levels:
            level_coord = _find_coord_name(
                ds,
                ("level", "lev", "pressure", "isobaric", "isobaricInhPa", "plev"),
            )
            if not level_coord:
                raise ValueError("文件中未找到垂直层次坐标")
            available = [float(value) for value in ds[level_coord].values.tolist()]
            missing = [float(level) for level in levels if float(level) not in available]
            if missing:
                raise ValueError(f"请求层次不存在: {missing}；可用层次: {available}")
            ds = ds.sel({level_coord: [float(level) for level in levels]})

        if area is not None:
            if len(area) != 4:
                raise ValueError("area 必须是 [north, west, south, east]")
            lat_name = _find_coord_name(ds, ("latitude", "lat"))
            lon_name = _find_coord_name(ds, ("longitude", "lon"))
            if not lat_name or not lon_name:
                raise ValueError("文件中未找到 latitude/longitude 坐标")
            north, west, south, east = [float(v) for v in area]
            lat_values = ds[lat_name].values
            lat_slice = (
                slice(north, south) if lat_values[0] > lat_values[-1] else slice(south, north)
            )
            lon_values = ds[lon_name].values
            lon_min = float(lon_values.min())
            lon_max = float(lon_values.max())
            if lon_min >= 0 and west < 0:
                west = west % 360
            if lon_min >= 0 and east < 0:
                east = east % 360
            if west <= east:
                ds = ds.sel({lat_name: lat_slice, lon_name: slice(west, east)})
            else:
                left = ds.sel({lat_name: lat_slice, lon_name: slice(west, lon_max)})
                right = ds.sel({lat_name: lat_slice, lon_name: slice(lon_min, east)})
                ds = xr.concat([left, right], dim=lon_name)

        empty_dims = {name: int(size) for name, size in ds.sizes.items() if int(size) == 0}
        if empty_dims:
            raise ValueError(f"裁剪结果为空: {empty_dims}")

        ds.to_netcdf(output_path)
        subset_sizes = {name: int(size) for name, size in ds.sizes.items()}
    finally:
        ds.close()

    return {
        "status": "success",
        "input_path": short_path(input_path),
        "output_path": str(output_path),
        "file_path": short_path(output_path),
        "file_size": output_path.stat().st_size,
        "original_dimensions": original_sizes,
        "dimensions": subset_sizes,
        "time_range": {"start": start_time, "end": end_time} if start_time or end_time else None,
        "area": area,
        "variables": variables,
        "levels": levels,
    }


def _find_coord_name(ds, candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in ds.coords or name in ds.dims:
            return name
    lowered = {str(name).lower(): str(name) for name in list(ds.coords) + list(ds.dims)}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None
