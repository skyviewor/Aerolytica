"""Small, deterministic GRIB/NetCDF operations for the managed Python runtime."""

from __future__ import annotations

import argparse
import json
from contextlib import ExitStack
from pathlib import Path


def _open_dataset(path: Path):
    import xarray as xr

    if path.suffix.lower() in {".grib", ".grb", ".grib2", ".grb2"}:
        return xr.open_dataset(path, engine="cfgrib")
    return xr.open_dataset(path)


def _latitude(ds) -> str | None:
    return next((name for name in ("latitude", "lat") if name in ds.coords), None)


def _longitude(ds) -> str | None:
    return next((name for name in ("longitude", "lon") if name in ds.coords), None)


def transform(
    operation: str,
    inputs: list[Path],
    output: Path,
    *,
    area: list[float] | None = None,
    variables: list[str] | None = None,
) -> dict:
    import numpy as np
    import xarray as xr

    if not inputs or (operation == "merge_time" and len(inputs) < 2):
        raise ValueError("需要输入文件；时间合并至少需要两个输入")
    if output.exists():
        raise FileExistsError(f"输出已存在：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        datasets = [stack.enter_context(_open_dataset(path)) for path in inputs]
        if operation == "merge_time":
            for ds in datasets:
                if "time" not in ds.coords:
                    raise ValueError("输入文件缺少 time 坐标")
            result = xr.concat(datasets, dim="time", combine_attrs="override").sortby("time")
        elif len(datasets) != 1:
            raise ValueError("该操作只接受一个输入文件")
        else:
            result = datasets[0]
        if variables:
            absent = set(variables) - set(result.data_vars)
            if absent:
                raise ValueError(f"变量不存在：{', '.join(sorted(absent))}")
            result = result[variables]
        if operation == "subset":
            if not area or len(area) != 4:
                raise ValueError("裁剪需提供 [north, west, south, east]")
            north, west, south, east = area
            lat, lon = _latitude(result), _longitude(result)
            if lat is None or lon is None:
                raise ValueError("数据缺少经纬度坐标")
            if west > east:
                raise ValueError("当前子集操作要求 west <= east")
            latitude = result[lat]
            if latitude.ndim != 1 or result[lon].ndim != 1:
                raise ValueError("当前子集操作只支持一维规则网格坐标")
            lat_slice = (
                slice(south, north)
                if latitude.values[0] <= latitude.values[-1]
                else slice(north, south)
            )
            result = result.sel({lat: lat_slice, lon: slice(west, east)})
        elif operation in {"mean_time", "sum_time"}:
            if "time" not in result.dims:
                raise ValueError("输入文件缺少 time 维度")
            result = (
                result.mean("time", keep_attrs=True)
                if operation == "mean_time"
                else result.sum("time", keep_attrs=True)
            )
        elif operation == "mean_area":
            lat, lon = _latitude(result), _longitude(result)
            if lat is None or lon is None or result[lat].ndim != 1 or result[lon].ndim != 1:
                raise ValueError("区域均值需要一维规则网格经纬度")
            weights = xr.DataArray(
                np.cos(np.deg2rad(result[lat])), coords={lat: result[lat]}, dims=[lat]
            )
            result = result.weighted(weights).mean((lat, lon), keep_attrs=True)
        elif operation not in {"convert", "merge_time", "subset"}:
            raise ValueError(f"不支持的操作：{operation}")
        if not all(size > 0 for size in result.sizes.values()):
            raise ValueError("结果为空，请检查时间和空间范围")
        temp = output.with_name(f".{output.name}.aero-tmp")
        try:
            result.to_netcdf(temp)
            temp.replace(output)
        finally:
            temp.unlink(missing_ok=True)
        return {
            "status": "success",
            "output": str(output),
            "variables": list(result.data_vars),
            "dimensions": {name: int(size) for name, size in result.sizes.items()},
            "size_bytes": output.stat().st_size,
        }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", required=True)
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--area", type=json.loads)
    parser.add_argument("--variables", type=json.loads)
    args = parser.parse_args(argv)
    try:
        payload = transform(
            args.operation,
            [Path(item) for item in args.input],
            Path(args.output),
            area=args.area,
            variables=args.variables,
        )
    except Exception as exc:
        payload = {"status": "error", "message": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
