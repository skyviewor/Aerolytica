"""Tests for local scientific data discovery and registration."""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest
import xarray as xr


def _fake_grib2_message() -> bytes:
    section4 = (11).to_bytes(4, "big") + bytes([4]) + b"\0\0\0\0\0\0"
    total_length = 16 + len(section4) + 4
    section0 = b"GRIB" + b"\0\0" + bytes([0, 2]) + total_length.to_bytes(8, "big")
    return section0 + section4 + b"7777"


def _write_netcdf(path):
    ds = xr.Dataset(
        {
            "temperature": (
                ("time", "lat", "lon"),
                np.array([[[1.0, np.nan]], [[3.0, 4.0]], [[5.0, 6.0]]]),
            )
        },
        coords={
            "time": np.array(["2024-01-01", "2024-01-01", "2024-01-03"], dtype="datetime64[ns]"),
            "lat": [40.0],
            "lon": [110.0, 111.0],
        },
    )
    ds.to_netcdf(path)


@pytest.mark.asyncio
async def test_scan_preview_reports_metadata_quality_and_does_not_register(monkeypatch, tmp_path):
    from aero.toolbox.tools.local_data import scan_local_files

    data = tmp_path / "data" / "nested"
    data.mkdir(parents=True)
    _write_netcdf(data / "sample.nc")
    (data / "stations.csv").write_text(
        "time,lat,lon,temperature\n2024-01-01T00:00:00,40,110,1\n2024-01-01T02:00:00,41,111,\n",
        encoding="utf-8",
    )
    (data / "broken.grib2").write_bytes(b"not grib")
    (data / "ignore.txt").write_text("ignore", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await scan_local_files("data")

    assert result["mode"] == "preview"
    assert result["file_count"] == 3
    assert result["registered_count"] == 0
    netcdf = next(item for item in result["candidates"] if item["format"] == "netcdf")
    assert netcdf["status"] == "warning"
    assert netcdf["dimensions"] == {"time": 3, "lat": 1, "lon": 2}
    assert netcdf["quality"]["variables"]["temperature"]["missing"] == 1
    assert {issue["code"] for issue in netcdf["quality"]["issues"]} >= {
        "duplicate_time",
        "irregular_time_interval",
    }
    csv_item = next(item for item in result["candidates"] if item["format"] == "csv")
    assert csv_item["quality"]["variables"]["temperature"]["missing"] == 1
    broken = next(item for item in result["candidates"] if item["format"] == "grib2")
    assert broken["status"] == "error"
    assert not (tmp_path / "aero_downloads.db").exists()


@pytest.mark.asyncio
async def test_confirm_registers_readable_files_once(monkeypatch, tmp_path):
    from aero.toolbox.tools.local_data import scan_local_files

    data = tmp_path / "data"
    data.mkdir()
    _write_netcdf(data / "sample.nc")
    monkeypatch.chdir(tmp_path)

    first = await scan_local_files("data", confirm=True)
    second = await scan_local_files("data", confirm=True)

    assert first["registered_count"] == 1
    assert second["registered_count"] == 0
    with sqlite3.connect(tmp_path / "aero_downloads.db") as connection:
        rows = connection.execute(
            "SELECT source, status, data_format, file_path, notes FROM cds_downloads"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][:3] == ("local", "confirmed", "netcdf")
    assert rows[0][3] == str((data / "sample.nc").resolve())
    assert "quality" in rows[0][4]


@pytest.mark.asyncio
async def test_scan_supports_valid_grib_and_pattern_filter(monkeypatch, tmp_path):
    from aero.toolbox.tools.local_data import scan_local_files

    data = tmp_path / "data"
    data.mkdir()
    (data / "forecast.grib2").write_bytes(_fake_grib2_message())
    (data / "table.csv").write_text("value\n1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    async def fake_lookup():
        return {}

    monkeypatch.setattr("aero.toolbox.tools.grib._load_gfs_parameter_lookup", fake_lookup)
    monkeypatch.setattr(
        "aero.toolbox.tools.grib._inspect_grib2_with_cfgrib",
        lambda _path: {"available": False, "message": "unavailable"},
    )

    result = await scan_local_files("data", pattern="*.grib2")

    assert result["file_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["format"] == "grib2"
    assert candidate["status"] == "warning"
    assert candidate["message_count"] == 1
    assert candidate["integrity"]["all_messages_end_with_7777"] is True


@pytest.mark.asyncio
async def test_scan_rejects_directory_outside_workspace(monkeypatch, tmp_path):
    from aero.toolbox.tools.local_data import scan_local_files

    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(project)

    result = await scan_local_files(str(outside))

    assert result["status"] == "error"
    assert "当前项目目录内" in result["message"]
