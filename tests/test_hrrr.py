"""Tests for NOAA HRRR AWS Open Data provider."""

from pathlib import Path

import httpx
import pytest

from aero.datasets.catalog import DatasetCatalog
from aero.datasets.models import DatasetDownloadRequest
from aero.datasets.providers.hrrr import DATASET_ID, HrrrProvider

IDX_TEXT = """1:0:d=2026061203:TMP:2 m above ground:anl:
2:10:d=2026061203:TMP:500 mb:anl:
3:20:d=2026061203:UGRD:500 mb:anl:
4:30:d=2026061203:VGRD:500 mb:anl:
"""


def test_hrrr_catalogue_exposes_forecast_dataset_and_common_variables():
    catalog = DatasetCatalog((HrrrProvider(),))

    assert catalog.search("HRRR")[0].dataset_id == DATASET_ID
    assert catalog.search("组合反射率")[0].dataset_id == DATASET_ID
    assert catalog.describe(DATASET_ID).spatial_resolution == "3 km"


@pytest.mark.asyncio
async def test_hrrr_download_uses_idx_ranges_for_requested_level(tmp_path):
    grib_url = "https://hrrr.test/hrrr.20260612/conus/hrrr.t03z.wrfprsf06.grib2"
    seen_ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".idx"):
            return httpx.Response(200, text=IDX_TEXT)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "40"})
        range_header = request.headers["range"]
        seen_ranges.append(range_header)
        start, end = range_header.removeprefix("bytes=").split("-")
        content = bytes(range(int(start), int(end) + 1))
        return httpx.Response(206, content=content)

    provider = HrrrProvider(
        base_url="https://hrrr.test",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=DATASET_ID,
            start_date="2026-06-12",
            end_date="2026-06-12",
            output_dir=tmp_path,
            variables=("TMP", "UGRD"),
            levels=(500.0,),
            times=("03:00",),
            forecast_hours=(6,),
            product="wrfprsf",
        )
    )

    assert seen_ranges == ["bytes=10-19", "bytes=20-29"]
    assert Path(result.files[0]).read_bytes() == bytes(range(10, 30))
    assert result.metadata["product"] == "wrfprsf"
    assert result.metadata["selected_messages"] == 2
    assert result.metadata["forecast_hours"] == [6]
    assert result.source_urls == (grib_url,)


@pytest.mark.asyncio
async def test_hrrr_resolves_last_message_range_from_remote_size(tmp_path):
    seen_ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".idx"):
            return httpx.Response(200, text=IDX_TEXT)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "40"})
        seen_ranges.append(request.headers["range"])
        return httpx.Response(206, content=b"0123456789")

    provider = HrrrProvider(
        base_url="https://hrrr.test",
        transport=httpx.MockTransport(handler),
    )
    await provider.download(
        DatasetDownloadRequest(
            dataset_id=DATASET_ID,
            start_date="2026-06-12",
            end_date="2026-06-12",
            output_dir=tmp_path,
            variables=("VGRD",),
            levels=(500.0,),
            times=("03",),
            forecast_hours=(0,),
            product="wrfprsf",
        )
    )

    assert seen_ranges == ["bytes=30-39"]


@pytest.mark.asyncio
async def test_hrrr_requires_variables_cycles_and_forecast_hours(tmp_path):
    provider = HrrrProvider(base_url="https://hrrr.test")
    base = dict(
        dataset_id=DATASET_ID,
        start_date="2026-06-12",
        end_date="2026-06-12",
        output_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="必须指定变量"):
        await provider.download(DatasetDownloadRequest(**base))
    with pytest.raises(ValueError, match="起报时次"):
        await provider.download(DatasetDownloadRequest(**base, variables=("TMP",)))
    with pytest.raises(ValueError, match="forecast_hours"):
        await provider.download(
            DatasetDownloadRequest(**base, variables=("TMP",), times=("03:00",))
        )
    with pytest.raises(ValueError, match="不支持产品"):
        await provider.download(
            DatasetDownloadRequest(
                **base,
                variables=("TMP",),
                times=("03:00",),
                forecast_hours=(0,),
                product="invalid",
            )
        )


@pytest.mark.asyncio
async def test_unified_download_tool_passes_hrrr_request_fields(monkeypatch, tmp_path):
    from aero.toolbox import builtin_tools

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".idx"):
            return httpx.Response(200, text=IDX_TEXT)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "40"})
        return httpx.Response(206, content=b"0123456789")

    provider = HrrrProvider(
        base_url="https://hrrr.test",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr("aero.datasets.catalog._DEFAULT_CATALOG", DatasetCatalog((provider,)))

    result = await builtin_tools.download_dataset(
        DATASET_ID,
        "2026-06-12",
        "2026-06-12",
        variables=["TMP"],
        levels=[500],
        times=["03:00"],
        forecast_hours=[6],
        product="wrfprsf",
        output_dir=str(tmp_path),
    )

    assert result["status"] == "success"
    assert result["metadata"]["cycles"] == ["03"]
    assert result["metadata"]["forecast_hours"] == [6]
