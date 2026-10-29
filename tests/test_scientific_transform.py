from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from aero.science.transform import transform


@pytest.mark.parametrize("tool_name", ["run_python_script", "transform_scientific_data"])
@pytest.mark.parametrize("mode", ["plan", "qa", "execute"])
def test_scientific_write_tools_require_execute_mode_and_approval(tool_name, mode):
    import aero.toolbox.builtin_tools  # noqa: F401
    from aero.data.modes import filter_tool_functions, is_tool_allowed
    from aero.toolbox.registry import get_registry

    tool = get_registry().get(tool_name)
    assert tool is not None and tool.requires_confirmation
    assert is_tool_allowed(tool_name, mode) is (mode == "execute")
    assert bool(filter_tool_functions([tool.to_llm_function()], mode)) is (mode == "execute")


@pytest.fixture
def netcdf_samples(tmp_path):
    paths = []
    for index, date in enumerate(("2025-01-01", "2025-01-02")):
        ds = xr.Dataset(
            {
                "temperature": (
                    ("time", "latitude", "longitude"),
                    np.full((1, 2, 2), 280.0 + index),
                    {"units": "K", "long_name": "air temperature"},
                )
            },
            coords={
                "time": [np.datetime64(date)],
                "latitude": [40.0, 30.0],
                "longitude": [100.0, 110.0],
            },
            attrs={"source": "fixed test sample"},
        )
        path = tmp_path / f"sample {index}.nc"
        ds.to_netcdf(path)
        paths.append(path)
    return paths


def test_merge_subset_and_mean_preserve_scientific_metadata(netcdf_samples, tmp_path):
    merged = tmp_path / "merged.nc"
    transform("merge_time", netcdf_samples[::-1], merged)
    with xr.open_dataset(merged) as ds:
        assert list(ds.time.dt.day.values) == [1, 2]
        assert ds.temperature.attrs["units"] == "K"
        assert ds.attrs["source"] == "fixed test sample"

    subset = tmp_path / "subset.nc"
    transform("subset", [merged], subset, area=[40, 99, 35, 105])
    with xr.open_dataset(subset) as ds:
        assert ds.sizes["latitude"] == 1
        assert ds.sizes["longitude"] == 1
        assert float(ds.latitude[0]) == 40.0

    averaged = tmp_path / "mean.nc"
    transform("mean_time", [merged], averaged)
    with xr.open_dataset(averaged) as ds:
        assert float(ds.temperature[0, 0]) == pytest.approx(280.5)
        assert ds.temperature.attrs["units"] == "K"


def test_transform_will_not_overwrite_existing_result(netcdf_samples):
    with pytest.raises(FileExistsError):
        transform("convert", [netcdf_samples[0]], netcdf_samples[1])
