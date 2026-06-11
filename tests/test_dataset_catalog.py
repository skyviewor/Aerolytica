"""Tests for the unified dataset catalogue and dataset providers."""

from pathlib import Path

import httpx
import pytest

from meteora.datasets.catalog import DatasetCatalog, get_dataset_catalog
from meteora.datasets.models import DatasetDownloadRequest
from meteora.datasets.providers.chirps import CHIRPS_SPEC, ChirpsProvider, months_between


def test_catalog_searches_dataset_variables_and_aliases():
    catalog = DatasetCatalog((ChirpsProvider(),))

    assert catalog.search("降水")[0].dataset_id == CHIRPS_SPEC.dataset_id
    assert catalog.search("precipitation")[0].provider_id == "ucsb-chg"
    assert catalog.search(domain="satellite") == ()


def test_dataset_tool_descriptions_use_catalogue_as_source_of_truth():
    from meteora.toolbox import builtin_tools  # noqa: F401
    from meteora.toolbox.registry import get_registry

    search = get_registry().get("search_datasets")
    download = get_registry().get("download_dataset")

    assert search is not None
    assert download is not None
    assert "所有内置支持的数据集都收录在这里" in search.description
    assert "准备下载任何数据前，先调用本工具" in search.description
    assert "download_tool=download_dataset" in download.description
    assert "查询结果中的 download_tool" in download.description


def test_default_catalogue_lists_all_supported_datasets_and_download_routes():
    datasets = get_dataset_catalog().list_datasets()
    routes = {dataset.dataset_id: dataset.download_tool for dataset in datasets}

    assert len(datasets) >= 14
    assert routes[CHIRPS_SPEC.dataset_id] == "download_dataset"
    assert routes["reanalysis-era5-pressure-levels"] == "download_era5"
    assert routes["reanalysis-era5-land"] == "download_era5"
    assert routes["gfs-global-forecast"] == "download_gfs"
    assert routes["gefs-global-ensemble-forecast"] == "download_gefs"
    assert routes["ifs-open-data-forecast"] == "download_ifs"
    assert routes["aifs-open-data-forecast"] == "download_ifs"
    assert routes["ncep-reanalysis-1-6-hourly"] == "download_dataset"
    assert routes["ncep-reanalysis-1-monthly"] == "download_dataset"
    assert routes["ncep-reanalysis-2-6-hourly"] == "download_dataset"
    assert routes["ncep-reanalysis-2-monthly"] == "download_dataset"
    assert routes["noaa-isd-global-hourly"] == "download_dataset"


@pytest.mark.asyncio
async def test_search_datasets_returns_download_route_for_every_supported_dataset():
    from meteora.toolbox import builtin_tools

    result = await builtin_tools.search_datasets()

    assert result["count"] >= 14
    assert all(dataset["download_tool"] for dataset in result["datasets"])
    assert {dataset["dataset_id"] for dataset in result["datasets"]} >= {
        CHIRPS_SPEC.dataset_id,
        "reanalysis-era5-pressure-levels",
        "gfs-global-forecast",
        "gefs-global-ensemble-forecast",
        "ifs-open-data-forecast",
        "aifs-open-data-forecast",
    }


@pytest.mark.asyncio
async def test_generic_download_rejects_catalogue_entry_with_dedicated_route(tmp_path):
    catalog = get_dataset_catalog()

    with pytest.raises(ValueError, match="download_gfs"):
        await catalog.download(
            DatasetDownloadRequest(
                dataset_id="gfs-global-forecast",
                start_date="2026-06-11",
                end_date="2026-06-11",
                output_dir=tmp_path,
            )
        )


def test_chirps_expands_cross_year_month_range():
    from datetime import date

    assert months_between(date(2025, 12, 30), date(2026, 2, 1)) == (
        (2025, 12),
        (2026, 1),
        (2026, 2),
    )


@pytest.mark.asyncio
async def test_chirps_download_resumes_partial_month_file(tmp_path):
    content = b"0123456789"
    ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chirps/"):
            return httpx.Response(
                200,
                text='<a href="chirps-v2.0.2025.01.days_p05.nc">January</a>',
            )
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(content))})
        range_header = request.headers.get("range", "")
        ranges.append(range_header)
        if range_header:
            offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
            return httpx.Response(
                206,
                content=content[offset:],
                headers={"content-length": str(len(content) - offset)},
            )
        return httpx.Response(200, content=content, headers={"content-length": str(len(content))})

    provider = ChirpsProvider(
        base_url="https://example.test/chirps",
        transport=httpx.MockTransport(handler),
    )
    part = tmp_path / "chirps-v2.0.2025.01.days_p05.nc.part"
    part.write_bytes(content[:4])
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=CHIRPS_SPEC.dataset_id,
            start_date="2025-01-03",
            end_date="2025-01-04",
            output_dir=tmp_path,
            area=(40, 100, 20, 120),
        )
    )

    assert ranges == ["bytes=4-"]
    assert Path(result.files[0]).read_bytes() == content
    assert result.metadata["requires_local_subset"] is True
    assert len(result.warnings) == 2


@pytest.mark.asyncio
async def test_chirps_download_reports_byte_progress(tmp_path):
    content = b"0123456789"
    progress: list[tuple[object, ...]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"content-length": str(len(content))})

    provider = ChirpsProvider(
        base_url="https://example.test/chirps",
        transport=httpx.MockTransport(handler),
    )
    await provider.download(
        DatasetDownloadRequest(
            dataset_id=CHIRPS_SPEC.dataset_id,
            start_date="2025-01-03",
            end_date="2025-01-04",
            output_dir=tmp_path,
        ),
        on_progress=lambda *args: progress.append(args) if len(args) == 2 else None,
    )

    assert progress
    assert progress[-1] == (len(content), len(content))


@pytest.mark.asyncio
async def test_chirps_download_checks_month_availability_before_download(tmp_path):
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.url.path.endswith("/by_month/"):
            return httpx.Response(
                200,
                text='<a href="chirps-v2.0.2026.04.days_p05.nc">April</a>',
            )
        raise AssertionError("download should not start for unavailable CHIRPS months")

    provider = ChirpsProvider(
        base_url="https://example.test/chirps/by_month",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ValueError) as exc_info:
        await provider.download(
            DatasetDownloadRequest(
                dataset_id=CHIRPS_SPEC.dataset_id,
                start_date="2026-05-01",
                end_date="2026-06-01",
                output_dir=tmp_path,
            )
        )

    message = str(exc_info.value)
    assert "2026-05" in message
    assert "2026-06" in message
    assert "2026-04 至 2026-04" in message
    assert "404" not in message
    assert requests == [("GET", "https://example.test/chirps/by_month/")]


@pytest.mark.asyncio
async def test_unified_dataset_tools_dispatch(monkeypatch, tmp_path):
    from meteora.toolbox import builtin_tools

    provider = ChirpsProvider(
        base_url="https://example.test/chirps",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"netcdf",
                headers={"content-length": "6"},
            )
        ),
    )
    catalog = DatasetCatalog((provider,))
    monkeypatch.setattr("meteora.datasets.catalog._DEFAULT_CATALOG", catalog)

    found = await builtin_tools.search_datasets(query="降水")
    described = await builtin_tools.describe_dataset(CHIRPS_SPEC.dataset_id)
    downloaded = await builtin_tools.download_dataset(
        CHIRPS_SPEC.dataset_id,
        "2025-01-01",
        "2025-01-02",
        output_dir=str(tmp_path / "lab" / "data"),
    )

    assert found["datasets"][0]["dataset_id"] == CHIRPS_SPEC.dataset_id
    assert described["dataset"]["supports_resume"] is True
    assert downloaded["status"] == "success"
    assert Path(downloaded["files"][0]).name == "chirps-v2.0.2025.01.days_p05.nc"
