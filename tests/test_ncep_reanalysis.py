"""Tests for NOAA PSL NCEP Reanalysis provider."""

from datetime import date
from pathlib import Path

import httpx
import pytest

from meteora.datasets.catalog import DatasetCatalog
from meteora.datasets.models import DatasetDownloadRequest
from meteora.datasets.providers.ncep_reanalysis import (
    NCEP_REANALYSIS_SPECS,
    PRODUCTS,
    CatalogEntry,
    NcepReanalysisProvider,
    parse_catalog_xml,
)

ROOT_CATALOG = """<?xml version="1.0"?>
<catalog xmlns="http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink">
  <catalogRef xlink:href="pressure/catalog.xml" name="pressure"/>
  <catalogRef xlink:href="surface/catalog.xml" name="surface"/>
</catalog>
"""

PRESSURE_CATALOG = """<?xml version="1.0"?>
<catalog xmlns="http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0">
  <dataset name="air.2020.nc" urlPath="Datasets/ncep.reanalysis/pressure/air.2020.nc"/>
</catalog>
"""

SURFACE_CATALOG = """<?xml version="1.0"?>
<catalog xmlns="http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0">
  <dataset name="air.2020.nc" urlPath="Datasets/ncep.reanalysis/surface/air.2020.nc"/>
</catalog>
"""


class InterruptedStream(httpx.AsyncByteStream):
    def __init__(self, chunk: bytes = b"0123") -> None:
        self.chunk = chunk

    async def __aiter__(self):
        yield self.chunk
        raise httpx.RemoteProtocolError("peer closed connection")


def test_ncep_reanalysis_specs_cover_both_versions_and_time_scales():
    assert {spec.dataset_id for spec in NCEP_REANALYSIS_SPECS} == {
        "ncep-reanalysis-1-6-hourly",
        "ncep-reanalysis-1-monthly",
        "ncep-reanalysis-2-6-hourly",
        "ncep-reanalysis-2-monthly",
    }
    assert all(spec.download_tool == "download_dataset" for spec in NCEP_REANALYSIS_SPECS)


def test_parse_catalog_xml_discovers_refs_categories_variables_and_years():
    entries, refs = parse_catalog_xml(PRESSURE_CATALOG, "ncep.reanalysis")

    assert refs == ()
    assert entries[0].category == "pressure"
    assert entries[0].variable == "air"
    assert entries[0].year == 2020

    _, root_refs = parse_catalog_xml(ROOT_CATALOG, "ncep.reanalysis")
    assert root_refs == ("pressure/catalog.xml", "surface/catalog.xml")


def test_variable_selection_requires_category_when_ambiguous():
    pressure, _ = parse_catalog_xml(PRESSURE_CATALOG, "ncep.reanalysis")
    surface, _ = parse_catalog_xml(SURFACE_CATALOG, "ncep.reanalysis")
    product = PRODUCTS["ncep-reanalysis-1-6-hourly"]

    with pytest.raises(ValueError, match="存在歧义"):
        NcepReanalysisProvider._select_entries(
            pressure + surface,
            ("air",),
            product,
            date(2020, 1, 1),
            date(2020, 1, 2),
        )

    selected = NcepReanalysisProvider._select_entries(
        pressure + surface,
        ("pressure/air",),
        product,
        date(2020, 1, 1),
        date(2020, 1, 2),
    )
    assert selected == pressure


def test_monthly_selection_excludes_long_term_climatology():
    entries = (
        CatalogEntry("pressure", "air", "Datasets/ncep.reanalysis.derived/pressure/air.mon.mean.nc", None),
        CatalogEntry("pressure", "air", "Datasets/ncep.reanalysis.derived/pressure/air.mon.ltm.nc", None),
    )
    selected = NcepReanalysisProvider._select_entries(
        entries,
        ("pressure/air",),
        PRODUCTS["ncep-reanalysis-1-monthly"],
        date(2020, 1, 1),
        date(2020, 12, 31),
    )

    assert selected == entries[:1]


def test_unique_abbreviated_variable_names_resolve_without_retry():
    entries = (
        CatalogEntry(
            "gaussian_grid",
            "air.2m.gauss",
            "Datasets/ncep.reanalysis2/gaussian_grid/air.2m.gauss.2025.nc",
            2025,
        ),
        CatalogEntry(
            "gaussian_grid",
            "pres.sfc.gauss",
            "Datasets/ncep.reanalysis2/gaussian_grid/pres.sfc.gauss.2025.nc",
            2025,
        ),
    )
    product = PRODUCTS["ncep-reanalysis-2-6-hourly"]

    air = NcepReanalysisProvider._select_entries(
        entries,
        ("air.2m",),
        product,
        date(2025, 6, 1),
        date(2025, 6, 1),
    )
    pressure = NcepReanalysisProvider._select_entries(
        entries,
        ("6hr/pres.sfc",),
        product,
        date(2025, 6, 1),
        date(2025, 6, 1),
    )

    assert air == entries[:1]
    assert pressure == entries[1:]


def test_six_hourly_selection_excludes_dailies_and_prefers_exact_category():
    entries = (
        CatalogEntry(
            "Dailies/pressure",
            "hgt",
            "Datasets/ncep.reanalysis2/Dailies/pressure/hgt.2025.nc",
            2025,
        ),
        CatalogEntry(
            "pressure",
            "hgt",
            "Datasets/ncep.reanalysis2/pressure/hgt.2025.nc",
            2025,
        ),
    )
    product = PRODUCTS["ncep-reanalysis-2-6-hourly"]

    assert NcepReanalysisProvider._select_entries(
        entries,
        ("hgt",),
        product,
        date(2025, 1, 1),
        date(2025, 1, 1),
    ) == entries[1:]
    assert NcepReanalysisProvider._select_entries(
        entries,
        ("pressure/hgt",),
        product,
        date(2025, 1, 1),
        date(2025, 1, 1),
    ) == entries[1:]


@pytest.mark.asyncio
async def test_variable_search_filters_to_requested_time_scale():
    provider = NcepReanalysisProvider()
    provider._catalog_cache["ncep-reanalysis-2-6-hourly"] = (
        CatalogEntry("Dailies/pressure", "hgt", "Dailies/pressure/hgt.2025.nc", 2025),
        CatalogEntry("pressure", "hgt", "pressure/hgt.2025.nc", 2025),
    )

    variables = await provider.search_variables("ncep-reanalysis-2-6-hourly", "hgt")

    assert variables == ("pressure/hgt",)


@pytest.mark.asyncio
async def test_unified_variable_search_tool_returns_filtered_ncep_variables(monkeypatch):
    from meteora.toolbox import builtin_tools

    provider = NcepReanalysisProvider()
    provider._catalog_cache["ncep-reanalysis-2-6-hourly"] = (
        CatalogEntry("Dailies/pressure", "hgt", "Dailies/pressure/hgt.2025.nc", 2025),
        CatalogEntry("pressure", "hgt", "pressure/hgt.2025.nc", 2025),
    )
    monkeypatch.setattr("meteora.datasets.catalog._DEFAULT_CATALOG", DatasetCatalog((provider,)))

    result = await builtin_tools.search_dataset_variables("ncep-reanalysis-2-6-hourly", "hgt")

    assert result == {
        "status": "success",
        "dataset_id": "ncep-reanalysis-2-6-hourly",
        "query": "hgt",
        "count": 1,
        "variables": ["pressure/hgt"],
    }


@pytest.mark.asyncio
async def test_ambiguous_download_returns_structured_variable_query_guidance(monkeypatch, tmp_path):
    from meteora.toolbox import builtin_tools

    provider = NcepReanalysisProvider()
    provider._catalog_cache["ncep-reanalysis-2-6-hourly"] = (
        CatalogEntry("pressure", "air", "pressure/air.2025.nc", 2025),
        CatalogEntry("surface", "air", "surface/air.2025.nc", 2025),
    )
    monkeypatch.setattr("meteora.datasets.catalog._DEFAULT_CATALOG", DatasetCatalog((provider,)))

    result = await builtin_tools.download_dataset(
        "ncep-reanalysis-2-6-hourly",
        "2025-01-01",
        "2025-01-01",
        variables=["air"],
        output_dir=str(tmp_path),
    )

    assert result["status"] == "error"
    assert result["retry_same_request"] is False
    assert result["suggested_tool"] == "search_dataset_variables"
    assert result["suggested_args"] == {
        "dataset_id": "ncep-reanalysis-2-6-hourly",
        "query": "air",
    }


def test_missing_variable_error_is_concise():
    entries = tuple(
        CatalogEntry("gaussian_grid", f"variable{i}.sfc.gauss", f"file{i}.2025.nc", 2025)
        for i in range(40)
    )

    with pytest.raises(ValueError) as exc_info:
        NcepReanalysisProvider._select_entries(
            entries,
            ("missing",),
            PRODUCTS["ncep-reanalysis-2-6-hourly"],
            date(2025, 1, 1),
            date(2025, 1, 1),
        )

    assert len(str(exc_info.value)) < 500


@pytest.mark.asyncio
async def test_ncss_download_contains_time_area_variable_and_level(tmp_path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/ncep.reanalysis/catalog.xml"):
            return httpx.Response(200, text=ROOT_CATALOG)
        if request.url.path.endswith("/pressure/catalog.xml"):
            return httpx.Response(200, text=PRESSURE_CATALOG)
        if request.url.path.endswith("/surface/catalog.xml"):
            return httpx.Response(200, text=SURFACE_CATALOG)
        if "/ncss/grid/" in request.url.path:
            return httpx.Response(200, content=b"netcdf-subset", headers={"content-length": "13"})
        raise AssertionError(str(request.url))

    provider = NcepReanalysisProvider(
        catalog_base="https://example.test/thredds/catalog/Datasets",
        file_base="https://example.test/thredds/fileServer",
        ncss_base="https://example.test/thredds/ncss/grid",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id="ncep-reanalysis-1-6-hourly",
            start_date="2020-01-01",
            end_date="2020-01-02",
            output_dir=tmp_path,
            variables=("pressure/air",),
            levels=(500.0,),
            area=(40.0, 100.0, 20.0, 120.0),
        )
    )

    ncss_request = next(request for request in requests if "/ncss/grid/" in request.url.path)
    assert ncss_request.url.params["var"] == "air"
    assert ncss_request.url.params["vertCoord"] == "500"
    assert ncss_request.url.params["north"] == "40.0"
    assert ncss_request.url.params["time_start"] == "2020-01-01T00:00:00Z"
    assert Path(result.files[0]).read_bytes() == b"netcdf-subset"
    assert result.metadata["server_subset"] is True


@pytest.mark.asyncio
async def test_ncss_failure_falls_back_to_source_file_and_local_subset(tmp_path):
    import pandas as pd
    import xarray as xr

    source = tmp_path / "source.nc"
    xr.Dataset(
        {
            "air": (
                ("time", "level", "lat", "lon"),
                [[[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]]],
            )
        },
        coords={
            "time": pd.date_range("2020-01-01", periods=1),
            "level": [850.0, 500.0],
            "lat": [20.0, 10.0],
            "lon": [100.0, 110.0],
        },
    ).to_netcdf(source, engine="scipy")
    source_bytes = source.read_bytes()
    source.unlink()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ncep.reanalysis/catalog.xml"):
            return httpx.Response(200, text=ROOT_CATALOG)
        if request.url.path.endswith("/pressure/catalog.xml"):
            return httpx.Response(200, text=PRESSURE_CATALOG)
        if request.url.path.endswith("/surface/catalog.xml"):
            return httpx.Response(200, text=SURFACE_CATALOG)
        if "/ncss/grid/" in request.url.path:
            return httpx.Response(503, text="unavailable")
        if "/fileServer/" in request.url.path:
            return httpx.Response(
                200,
                content=source_bytes,
                headers={"content-length": str(len(source_bytes))},
            )
        raise AssertionError(str(request.url))

    provider = NcepReanalysisProvider(
        catalog_base="https://example.test/thredds/catalog/Datasets",
        file_base="https://example.test/thredds/fileServer",
        ncss_base="https://example.test/thredds/ncss/grid",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id="ncep-reanalysis-1-6-hourly",
            start_date="2020-01-01",
            end_date="2020-01-01",
            output_dir=tmp_path / "output",
            variables=("pressure/air",),
            levels=(500.0,),
            area=(20.0, 100.0, 10.0, 110.0),
        )
    )

    assert result.metadata["server_subset"] is False
    assert len(result.metadata["fallbacks"]) == 1
    assert (
        tmp_path
        / "output"
        / ".meteora-cache"
        / "ncep"
        / "ncep.reanalysis"
        / "pressure"
        / "air.2020.nc"
    ).exists()
    with xr.open_dataset(result.files[0]) as dataset:
        assert dataset.level.values.tolist() == [500.0]


@pytest.mark.asyncio
async def test_source_download_resumes_after_connection_closes(monkeypatch, tmp_path):
    first_chunk = b"a" * (1024 * 1024)
    content = first_chunk + b"456789"
    ranges: list[str] = []

    async def no_sleep(delay):
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        range_header = request.headers.get("range", "")
        ranges.append(range_header)
        if len(ranges) == 1:
            return httpx.Response(
                200,
                headers={"content-length": str(len(content))},
                stream=InterruptedStream(first_chunk),
            )
        offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
        return httpx.Response(
            206,
            content=content[offset:],
            headers={"content-length": str(len(content) - offset)},
        )

    monkeypatch.setattr("meteora.datasets.providers.ncep_reanalysis.asyncio.sleep", no_sleep)
    destination = tmp_path / "source.nc"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await NcepReanalysisProvider._download_stream(
            client,
            "https://example.test/source.nc",
            destination,
            None,
            resume=True,
        )

    assert ranges[0] == ""
    assert ranges[1].startswith("bytes=")
    assert int(ranges[1].removeprefix("bytes=").removesuffix("-")) > 0
    assert destination.read_bytes() == content


@pytest.mark.asyncio
async def test_ncss_download_restarts_after_connection_closes(monkeypatch, tmp_path):
    ranges: list[str] = []

    async def no_sleep(delay):
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        ranges.append(request.headers.get("range", ""))
        if len(ranges) == 1:
            return httpx.Response(
                200,
                headers={"content-length": "10"},
                stream=InterruptedStream(),
            )
        return httpx.Response(200, content=b"0123456789", headers={"content-length": "10"})

    monkeypatch.setattr("meteora.datasets.providers.ncep_reanalysis.asyncio.sleep", no_sleep)
    destination = tmp_path / "subset.nc"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await NcepReanalysisProvider._download_stream(
            client,
            "https://example.test/subset.nc",
            destination,
            None,
        )

    assert ranges == ["", ""]
    assert destination.read_bytes() == b"0123456789"


def test_cross_dateline_area_is_split():
    assert NcepReanalysisProvider._split_area((30, 170, -10, -170)) == (
        (30, 170, -10, 180.0),
        (30, -180.0, -10, -170),
    )


@pytest.mark.asyncio
async def test_reanalysis_rejects_dates_before_product_start(tmp_path):
    provider = NcepReanalysisProvider()
    with pytest.raises(ValueError, match="1948-01-01"):
        await provider.download(
            DatasetDownloadRequest(
                dataset_id="ncep-reanalysis-1-6-hourly",
                start_date="1947-01-01",
                end_date="1947-01-02",
                output_dir=tmp_path,
                variables=("pressure/air",),
            )
        )
