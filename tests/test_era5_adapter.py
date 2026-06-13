"""Tests for CDS adapter download flow."""

import zipfile

import pytest

from aero.adapters.cds_adapter import CDSAdapter, _detect_file_format


@pytest.mark.asyncio
async def test_download_single_day_request(tmp_path, monkeypatch):

    async def fake_submit(self, *args, **kwargs):
        dataset_id = kwargs.get("dataset_id", args[0] if args else "")
        ds_short = dataset_id.replace("reanalysis-", "").replace("derived-", "")
        var_str = "_".join(kwargs.get("variables", []))
        pl_str = "sfc"
        date_str = f"{kwargs['year']}{kwargs['month']:02d}{kwargs['day']:02d}"
        filename = f"cds_{ds_short}_{var_str}_{pl_str}_{date_str}.nc"
        return {
            "download_url": "https://example.com/data.nc",
            "dest_path": tmp_path / filename,
            "request_id": "req-123",
            "total_bytes": 0,
            "accept_ranges": "",
        }

    async def fake_fetch(self, download_url, dest_path, **kwargs):
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"CDF " + b"\0" * 2048)
        return 4096

    monkeypatch.setattr(CDSAdapter, "submit", fake_submit)
    monkeypatch.setattr(CDSAdapter, "fetch", fake_fetch)

    adapter = CDSAdapter(cds_url="https://example.com", cds_key="secret")
    result = await adapter.download(
        dataset_id="reanalysis-era5-single-levels",
        variables=["total_precipitation"],
        year=2025,
        month=7,
        day=8,
        dest_dir=tmp_path,
        area=[26, 102, 24, 103.5],
    )

    assert result.source == "cds"
    assert result.time_range == {"year": 2025, "month": "07", "day": 8}
    assert result.region == {"north": 26, "west": 102, "south": 24, "east": 103.5}
    assert "total_precipitation" in result.file_path.name
    assert result.file_path.name.startswith("cds_")
    assert result.params["requested_data_format"] == "netcdf"
    assert result.params["requested_download_format"] == "unarchived"
    assert result.params["actual_file_format"] == "netcdf3"
    assert result.params["dataset"] == "reanalysis-era5-single-levels"


@pytest.mark.asyncio
async def test_download_unpacks_netcdf_zip(tmp_path, monkeypatch):
    async def fake_submit(self, *args, **kwargs):
        return {
            "download_url": "https://example.com/data.zip",
            "dest_path": tmp_path / "test.nc",
            "request_id": "req-456",
            "total_bytes": 0,
            "accept_ranges": "",
        }

    async def fake_fetch(self, download_url, dest_path, **kwargs):
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        inner = tmp_path / "inner.nc"
        inner.write_bytes(b"CDF " + b"\0" * 2048)
        with zipfile.ZipFile(dest_path, "w") as archive:
            archive.write(inner, arcname="data.nc")
        from aero.adapters.cds_adapter import _normalize_downloaded_file

        _normalize_downloaded_file(dest_path)
        return dest_path.stat().st_size

    monkeypatch.setattr(CDSAdapter, "submit", fake_submit)
    monkeypatch.setattr(CDSAdapter, "fetch", fake_fetch)

    adapter = CDSAdapter(cds_url="https://example.com", cds_key="secret")
    result = await adapter.download(
        dataset_id="reanalysis-era5-single-levels",
        variables=["2m_temperature"],
        year=2025,
        month=7,
        day=8,
        dest_dir=tmp_path,
    )

    assert result.file_path.read_bytes().startswith(b"CDF")
    assert _detect_file_format(result.file_path) == "netcdf3"
    assert result.params["actual_file_format"] == "netcdf3"


def test_normalize_downloaded_zip_prefers_actual_grib_file(tmp_path):
    from aero.adapters.cds_adapter import _normalize_downloaded_file

    dest_path = tmp_path / "download.nc"
    bogus_netcdf = tmp_path / "a.nc"
    grib = tmp_path / "b.grib"
    bogus_netcdf.write_bytes(b"not a netcdf")
    grib.write_bytes(b"GRIB" + b"\0" * 2048)

    with zipfile.ZipFile(dest_path, "w") as archive:
        archive.write(bogus_netcdf, arcname="a.nc")
        archive.write(grib, arcname="b.grib")

    _normalize_downloaded_file(dest_path)

    assert dest_path.read_bytes().startswith(b"GRIB")
    assert _detect_file_format(dest_path) == "grib"


@pytest.mark.asyncio
async def test_fetch_reuses_complete_existing_file(tmp_path, monkeypatch):
    dest = tmp_path / "complete.nc"
    dest.write_bytes(b"CDF " + b"\0" * 2048)

    def fail_download(*args, **kwargs):
        raise AssertionError("should not download when local file is already complete")

    monkeypatch.setattr(CDSAdapter, "_download_file", staticmethod(fail_download))

    adapter = CDSAdapter(cds_url="https://example.com", cds_key="secret")
    size = await adapter.fetch(
        download_url="https://example.com/data.nc",
        dest_path=dest,
        total_bytes=dest.stat().st_size,
    )

    assert size == dest.stat().st_size
    assert _detect_file_format(dest) == "netcdf3"


@pytest.mark.asyncio
async def test_subset_netcdf_crops_time_and_area(tmp_path):
    import pandas as pd
    import xarray as xr

    from aero.toolbox.builtin_tools import subset_netcdf

    source = tmp_path / "month.nc"
    times = pd.date_range("2019-02-01T00:00", periods=48, freq="h")
    xr.Dataset(
        {
            "t2m": (
                ("time", "latitude", "longitude"),
                [
                    [[float(i + j + k) for k in range(3)] for j in range(3)]
                    for i in range(48)
                ],
            )
        },
        coords={
            "time": times,
            "latitude": [1.0, 0.0, -1.0],
            "longitude": [100.0, 101.0, 102.0],
        },
    ).to_netcdf(source, engine="scipy")

    output = tmp_path / "subset.nc"
    result = await subset_netcdf(
        input_path=str(source),
        output_path=str(output),
        start_time="2019-02-02T00:00",
        end_time="2019-02-02T23:00",
        area=[1.0, 100.0, 0.0, 101.0],
    )

    assert result["status"] == "success"
    assert output.exists()

    ds = xr.open_dataset(output)
    try:
        assert ds.sizes["time"] == 24
        assert ds.sizes["latitude"] == 2
        assert ds.sizes["longitude"] == 2
        assert str(ds.time.values[0]).startswith("2019-02-02T00:00")
        assert str(ds.time.values[-1]).startswith("2019-02-02T23:00")
    finally:
        ds.close()


@pytest.mark.asyncio
async def test_subset_netcdf_crops_vertical_levels(tmp_path):
    import xarray as xr

    from aero.toolbox.builtin_tools import subset_netcdf

    source = tmp_path / "pressure.nc"
    xr.Dataset(
        {"air": (("level", "latitude", "longitude"), [[[1.0]], [[2.0]], [[3.0]]])},
        coords={"level": [1000.0, 850.0, 500.0], "latitude": [0.0], "longitude": [100.0]},
    ).to_netcdf(source, engine="scipy")

    output = tmp_path / "pressure_subset.nc"
    result = await subset_netcdf(
        input_path=str(source),
        output_path=str(output),
        levels=[850.0, 500.0],
    )

    assert result["status"] == "success"
    with xr.open_dataset(output) as dataset:
        assert dataset.level.values.tolist() == [850.0, 500.0]
