"""Tests for NOAA ISD Global Hourly provider."""

from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from aero.datasets.catalog import DatasetCatalog
from aero.datasets.models import DatasetDownloadRequest, DatasetStation
from aero.datasets.providers.noaa_isd import (
    DATASET_ID,
    ISD_SPEC,
    MAX_AUTO_STATIONS,
    NoaaIsdProvider,
    parse_station_history,
    parse_station_inventory,
)

HISTORY = """"USAF","WBAN","STATION NAME","CTRY","STATE","ICAO","LAT","LON","ELEV(M)","BEGIN","END"
"725030","14732","LAGUARDIA AIRPORT","US","NY","KLGA","+40.779","-073.880","+0003.0","19480101","20250825"
"725050","04781","ISLIP AIRPORT","US","NY","KISP","+40.795","-073.100","+0030.0","19600101","20250825"
"999999","00001","DATELINE EAST","ZZ","","DLEE","+10.000","+179.000","+0001.0","20000101","20250825"
"999999","00002","DATELINE WEST","ZZ","","DLEW","+10.000","-179.000","+0001.0","20000101","20250825"
"""

INVENTORY = """"USAF","WBAN","YEAR","JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"
"725030","14732","2025","10","0","0","0","0","0","0","0","0","0","0","0"
"725050","04781","2025","10","0","0","0","0","0","0","0","0","0","0","0"
"999999","00001","2025","10","0","0","0","0","0","0","0","0","0","0","0"
"999999","00002","2025","10","0","0","0","0","0","0","0","0","0","0","0"
"""

ARCHIVE_CSV = """"STATION","DATE","SOURCE","REPORT_TYPE","CALL_SIGN","QUALITY_CONTROL","TMP","WND"
"72503014732","2025-01-01T00:00:00","4","FM-12","KLGA","V020","+0089,1","080,1,N,0051,1"
"72503014732","2025-01-02T00:00:00","4","FM-12","KLGA","V020","+0090,1","090,1,N,0052,1"
"""


def _catalog_response(request: httpx.Request) -> httpx.Response | None:
    if request.url.path.endswith("isd-history.csv"):
        return httpx.Response(200, text=HISTORY, headers={"etag": "history"})
    if request.url.path.endswith("isd-inventory.csv"):
        return httpx.Response(200, text=INVENTORY, headers={"etag": "inventory"})
    return None


def _search_payload(fields: tuple[str, ...] = ("TMP", "WND")) -> dict:
    return {
        "results": [{"filePath": "/data/global-hourly/access/2025/72503014732.csv"}],
        "dataTypes": {"buckets": [{"key": field} for field in fields]},
    }


def test_station_catalog_parsers_preserve_metadata_and_month_counts():
    stations = parse_station_history(HISTORY)
    inventory = parse_station_inventory(INVENTORY)

    assert stations[0].station_id == "72503014732"
    assert stations[0].icao == "KLGA"
    assert stations[0].latitude == 40.779
    assert inventory["72503014732"][(2025, 1)] == 10
    assert inventory["72503014732"][(2025, 2)] == 0


@pytest.mark.asyncio
async def test_station_search_supports_query_area_dates_and_dateline(tmp_path):
    provider = NoaaIsdProvider(
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(lambda request: _catalog_response(request)),
    )

    named = await provider.search_stations(DATASET_ID, "KLGA", start_date="2025-01-01")
    dateline = await provider.search_stations(
        DATASET_ID,
        area=(20, 170, 0, -170),
        start_date="2025-01-01",
        end_date="2025-01-31",
    )

    assert [station.station_id for station in named] == ["72503014732"]
    assert {station.station_id for station in dateline} == {"99999900001", "99999900002"}
    assert named[0].monthly_observations == 10


@pytest.mark.asyncio
async def test_station_catalog_uses_conditional_cache_and_stale_fallback(tmp_path):
    cache_dir = tmp_path / "cache"
    first = NoaaIsdProvider(
        cache_dir=cache_dir,
        transport=httpx.MockTransport(lambda request: _catalog_response(request)),
    )
    await first.search_stations(DATASET_ID, "KLGA")
    conditional_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        conditional_headers.append(request.headers.get("if-none-match", ""))
        return httpx.Response(503)

    second = NoaaIsdProvider(
        cache_dir=cache_dir,
        transport=httpx.MockTransport(handler),
    )
    stations = await second.search_stations(DATASET_ID, "KLGA")

    assert stations[0].station_id == "72503014732"
    assert conditional_headers == ["history", "inventory"]


@pytest.mark.asyncio
async def test_variable_search_understands_natural_language_alias():
    provider = NoaaIsdProvider()

    assert await provider.search_variables(DATASET_ID, "气温") == ("TMP: air temperature",)
    assert await provider.search_variables(DATASET_ID, "wind") == ("WND: wind observation",)


def test_area_download_rejects_more_than_fifty_stations():
    provider = NoaaIsdProvider()
    stations = tuple(
        DatasetStation(
            station_id=f"{index:011d}",
            name=f"Station {index}",
            latitude=10,
            longitude=10,
            begin_date="2000-01-01",
            end_date="2025-12-31",
        )
        for index in range(MAX_AUTO_STATIONS + 1)
    )
    inventory = {
        station.station_id: {(2025, 1): 1}
        for station in stations
    }

    with pytest.raises(ValueError, match="超过自动下载上限"):
        provider._select_stations(
            stations,
            inventory,
            DatasetDownloadRequest(
                dataset_id=DATASET_ID,
                start_date="2025-01-01",
                end_date="2025-01-01",
                output_dir=Path("data"),
                area=(20, 0, 0, 20),
            ),
            date(2025, 1, 1),
            date(2025, 1, 1),
        )


@pytest.mark.asyncio
async def test_download_uses_ncei_precise_station_subset(tmp_path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        catalog = _catalog_response(request)
        if catalog is not None:
            return catalog
        if "/search/" in request.url.path:
            return httpx.Response(200, json=_search_payload())
        if "/access/services/data/" in request.url.path:
            return httpx.Response(200, text=ARCHIVE_CSV)
        raise AssertionError(str(request.url))

    provider = NoaaIsdProvider(
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=DATASET_ID,
            start_date="2025-01-01",
            end_date="2025-01-01",
            output_dir=tmp_path / "output",
            stations=("KLGA",),
            variables=("temperature", "WND"),
        )
    )

    data_request = next(
        request for request in requests if "/access/services/data/" in request.url.path
    )
    params = parse_qs(data_request.url.query.decode())
    assert params["stations"] == ["72503014732"]
    assert params["dataTypes"] == ["TMP,WND"]
    assert Path(result.files[0]).name == (
        "noaa-isd-global-hourly_72503014732_2025-01-01_2025-01-01_parsed.csv"
    )
    assert Path(result.metadata["raw_files"][0]).name == (
        "noaa-isd-global-hourly_72503014732_2025-01-01_2025-01-01.csv"
    )
    assert "temperature_c" in Path(result.files[0]).read_text().splitlines()[0]
    assert result.metadata["server_subset"] is True


@pytest.mark.asyncio
async def test_download_falls_back_to_yearly_archive_and_local_subset(tmp_path):
    ranges: list[str] = []
    archive = ARCHIVE_CSV.encode()

    def handler(request: httpx.Request) -> httpx.Response:
        catalog = _catalog_response(request)
        if catalog is not None:
            return catalog
        if "/search/" in request.url.path:
            return httpx.Response(200, json=_search_payload())
        if "/services/data/" in request.url.path:
            return httpx.Response(503)
        if request.url.path.endswith("/2025/72503014732.csv"):
            range_header = request.headers.get("range", "")
            ranges.append(range_header)
            offset = int(range_header.removeprefix("bytes=").removesuffix("-")) if range_header else 0
            return httpx.Response(
                206 if offset else 200,
                content=archive[offset:],
                headers={"content-length": str(len(archive) - offset)},
            )
        raise AssertionError(str(request.url))

    provider = NoaaIsdProvider(
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(handler),
    )
    part = (
        tmp_path
        / "output"
        / ".aero-cache"
        / "isd"
        / "2025"
        / "72503014732.csv.part"
    )
    part.parent.mkdir(parents=True)
    part.write_bytes(archive[:20])
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=DATASET_ID,
            start_date="2025-01-01",
            end_date="2025-01-01",
            output_dir=tmp_path / "output",
            stations=("72503014732",),
            variables=("TMP",),
        )
    )

    text = Path(result.files[0]).read_text()
    assert "2025-01-01T00:00:00" in text
    assert "2025-01-02T00:00:00" not in text
    assert "temperature_c" in text.splitlines()[0]
    raw_text = Path(result.metadata["raw_files"][0]).read_text()
    assert "TMP" in raw_text.splitlines()[0]
    assert "WND" not in raw_text.splitlines()[0]
    assert ranges == ["bytes=20-"]
    assert result.metadata["server_subset"] is False


@pytest.mark.asyncio
async def test_cached_raw_download_recreates_missing_readable_table(tmp_path):
    data_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal data_requests
        catalog = _catalog_response(request)
        if catalog is not None:
            return catalog
        if "/search/" in request.url.path:
            return httpx.Response(200, json=_search_payload())
        if "/access/services/data/" in request.url.path:
            data_requests += 1
            return httpx.Response(200, text=ARCHIVE_CSV)
        raise AssertionError(str(request.url))

    provider = NoaaIsdProvider(
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(handler),
    )
    request = DatasetDownloadRequest(
        dataset_id=DATASET_ID,
        start_date="2025-01-01",
        end_date="2025-01-01",
        output_dir=tmp_path / "output",
        stations=("72503014732",),
        variables=("TMP",),
    )
    first = await provider.download(request)
    Path(first.files[0]).unlink()

    second = await provider.download(request)

    assert data_requests == 1
    assert Path(second.files[0]).exists()
    assert "temperature_c" in Path(second.files[0]).read_text().splitlines()[0]
    assert second.reused_files == ()


@pytest.mark.asyncio
async def test_one_station_failure_does_not_discard_successful_station(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        catalog = _catalog_response(request)
        if catalog is not None:
            return catalog
        params = parse_qs(request.url.query.decode())
        station = params.get("stations", [""])[0]
        if "/search/" in request.url.path:
            fields = ("TMP",) if station == "72503014732" else ("WND",)
            return httpx.Response(200, json=_search_payload(fields))
        if "/access/services/data/" in request.url.path:
            return httpx.Response(200, text=ARCHIVE_CSV)
        raise AssertionError(str(request.url))

    provider = NoaaIsdProvider(
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=DATASET_ID,
            start_date="2025-01-01",
            end_date="2025-01-01",
            output_dir=tmp_path / "output",
            stations=("KLGA", "KISP"),
            variables=("TMP",),
        )
    )

    assert len(result.files) == 1
    assert result.metadata["failed_stations"][0]["station"] == "72505004781"
    assert result.warnings


@pytest.mark.asyncio
async def test_unified_station_tool_and_catalog_route(monkeypatch, tmp_path):
    from aero.toolbox import builtin_tools

    provider = NoaaIsdProvider(
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(lambda request: _catalog_response(request)),
    )
    monkeypatch.setattr("aero.datasets.catalog._DEFAULT_CATALOG", DatasetCatalog((provider,)))

    result = await builtin_tools.search_dataset_stations(DATASET_ID, "LAGUARDIA")

    assert ISD_SPEC.download_tool == "download_dataset"
    assert result["status"] == "success"
    assert result["stations"][0]["station_id"] == "72503014732"
