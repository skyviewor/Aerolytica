"""Tests for NOAA GHCN-Daily AWS Open Data provider."""

import csv
import gzip
from pathlib import Path

import httpx
import pytest

from aero.datasets.models import DatasetDownloadRequest
from aero.datasets.providers.ghcn_daily import (
    DATASET_ID,
    GhcnDailyProvider,
    parse_countries,
    parse_station_inventory,
    parse_stations,
)

COUNTRIES = "US United States\nCA Canada\n"
INVENTORY = (
    "USW00094728  40.7789  -73.9692 TMAX 1869 2026\n"
    "USW00094728  40.7789  -73.9692 PRCP 1869 2026\n"
    "CA001234567  45.0000  -75.0000 TMAX 2000 2020\n"
)
STATIONS = (
    "USW00094728  40.7789  -73.9692   39.6 NY NEW YORK CENTRAL PARK OBS BELVEDERE TOWER\n"
    "CA001234567  45.0000  -75.0000  100.0 ON OTTAWA TEST STATION            \n"
)
STATION_CSV = """ID,DATE,ELEMENT,DATA_VALUE,M_FLAG,Q_FLAG,S_FLAG,OBS_TIME
USW00094728,20260611,TMAX,256,,,Z,
USW00094728,20260612,TMAX,267,,,Z,
USW00094728,20260612,PRCP,12,,,Z,
USW00094728,20260613,TMAX,278,,,Z,
"""
NCEI_CSV = (
    '"STATION","DATE","LATITUDE","LONGITUDE","ELEVATION","NAME",'
    '"PRCP","PRCP_ATTRIBUTES","TMAX","TMAX_ATTRIBUTES"\n'
    '"USW00094728","2026-06-12","40.7789","-73.9692","39.6","CENTRAL PARK",'
    '"12",",,W","267",",,W"\n'
)


def _gzip_content(text: str) -> bytes:
    return gzip.compress(text.encode())


def _provider(tmp_path: Path) -> GhcnDailyProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("ghcnd-countries.txt"):
            return httpx.Response(200, text=COUNTRIES)
        if request.url.path.endswith("ghcnd-inventory.txt"):
            return httpx.Response(200, text=INVENTORY)
        if request.url.path.endswith("ghcnd-stations.txt"):
            return httpx.Response(200, text=STATIONS)
        if request.url.path.endswith("USW00094728.csv.gz"):
            return httpx.Response(200, content=_gzip_content(STATION_CSV))
        return httpx.Response(404)

    return GhcnDailyProvider(
        base_url="https://ghcn.test",
        access_base="https://ghcn.test/access",
        cache_dir=tmp_path / "catalog-cache",
        transport=httpx.MockTransport(handler),
    )


def test_ghcn_fixed_width_catalog_parsers():
    inventory = parse_station_inventory(INVENTORY)
    stations = parse_stations(STATIONS, parse_countries(COUNTRIES), inventory)

    assert inventory["USW00094728"]["TMAX"] == (1869, 2026)
    assert stations[0].name == "NEW YORK CENTRAL PARK OBS BELV"
    assert stations[0].country == "United States"
    assert stations[0].state == "NY"
    assert stations[0].begin_date == "1869-01-01"
    assert stations[0].end_date == "2026-12-31"


@pytest.mark.asyncio
async def test_ghcn_station_and_variable_search(tmp_path):
    provider = _provider(tmp_path)

    stations = await provider.search_stations(
        DATASET_ID,
        query="central park",
        start_date="2026-06-12",
        end_date="2026-06-12",
    )
    variables = await provider.search_variables(DATASET_ID, "降水")

    assert [station.station_id for station in stations] == ["USW00094728"]
    assert variables == ("PRCP: daily precipitation (mm)",)


@pytest.mark.asyncio
async def test_ghcn_download_filters_and_decodes_station_file(tmp_path):
    provider = _provider(tmp_path)
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=DATASET_ID,
            start_date="2026-06-12",
            end_date="2026-06-12",
            output_dir=tmp_path / "data",
            stations=("USW00094728",),
            variables=("TMAX", "降水"),
        )
    )

    with Path(result.files[0]).open(newline="") as input_file:
        rows = list(csv.DictReader(input_file))
    assert [(row["element"], row["value"], row["unit"]) for row in rows] == [
        ("TMAX", "26.7", "°C"),
        ("PRCP", "1.2", "mm"),
    ]
    assert Path(result.metadata["raw_files"][0]).suffix == ".gz"
    assert result.metadata["server_subset"] is False
    assert result.metadata["requires_local_subset"] is False


@pytest.mark.asyncio
async def test_ghcn_requires_station_or_area(tmp_path):
    with pytest.raises(ValueError, match="stations 或 area"):
        await _provider(tmp_path).download(
            DatasetDownloadRequest(
                dataset_id=DATASET_ID,
                start_date="2026-06-12",
                end_date="2026-06-12",
                output_dir=tmp_path,
            )
        )


@pytest.mark.asyncio
async def test_ghcn_accepts_station_suffix_and_partial_date_overlap(tmp_path):
    provider = _provider(tmp_path)

    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=DATASET_ID,
            start_date="2026-06-12",
            end_date="2027-06-12",
            output_dir=tmp_path / "data",
            stations=("94728",),
            variables=("TMAX",),
        )
    )

    assert result.metadata["actual_ranges"]["USW00094728"] == {
        "start_date": "2026-06-12",
        "end_date": "2026-06-12",
    }


@pytest.mark.asyncio
async def test_unified_download_tool_accepts_single_station_id(monkeypatch, tmp_path):
    from aero.datasets.catalog import DatasetCatalog
    from aero.toolbox import builtin_tools

    provider = _provider(tmp_path)
    monkeypatch.setattr("aero.datasets.catalog._DEFAULT_CATALOG", DatasetCatalog((provider,)))

    result = await builtin_tools.download_dataset(
        DATASET_ID,
        "2026-06-12",
        "2026-06-12",
        variables=["TMAX"],
        station_id="USW00094728",
        output_dir=str(tmp_path / "data"),
    )

    assert result["status"] == "success"
    assert result["metadata"]["stations"][0]["station_id"] == "USW00094728"


@pytest.mark.asyncio
async def test_ghcn_prefers_current_ncei_station_csv(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("ghcnd-countries.txt"):
            return httpx.Response(200, text=COUNTRIES)
        if request.url.path.endswith("ghcnd-inventory.txt"):
            return httpx.Response(200, text=INVENTORY)
        if request.url.path.endswith("ghcnd-stations.txt"):
            return httpx.Response(200, text=STATIONS)
        if request.url.path.endswith("USW00094728.csv"):
            return httpx.Response(200, text=NCEI_CSV)
        return httpx.Response(404)

    provider = GhcnDailyProvider(
        base_url="https://ghcn.test",
        access_base="https://ncei.test/access",
        cache_dir=tmp_path / "catalog-cache",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=DATASET_ID,
            start_date="2026-06-12",
            end_date="2026-06-12",
            output_dir=tmp_path / "data",
            stations=("USW00094728",),
            variables=("TMAX", "PRCP"),
        )
    )

    assert result.source_urls == ("https://ncei.test/access/USW00094728.csv",)
    assert result.metadata["actual_variables_by_station"]["USW00094728"] == ["PRCP", "TMAX"]
