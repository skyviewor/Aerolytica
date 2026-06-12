"""Tests for NOAA MRMS AWS Open Data provider."""

from pathlib import Path

import httpx
import pytest

from aero.datasets.catalog import DatasetCatalog
from aero.datasets.models import DatasetDownloadRequest
from aero.datasets.providers.mrms import DATASET_ID, MrmsProvider, parse_s3_listing


def listing(objects: tuple[tuple[str, int], ...], token: str = "") -> str:
    contents = "".join(
        f"<Contents><Key>{key}</Key><Size>{size}</Size></Contents>" for key, size in objects
    )
    next_token = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    return f"<ListBucketResult>{contents}{next_token}</ListBucketResult>"


def test_parse_mrms_s3_listing_reads_objects_and_continuation_token():
    objects, token = parse_s3_listing(
        listing((("CONUS/PrecipRate_00.00/20260612/file.grib2.gz", 12),), "next-page")
    )

    assert objects == (("CONUS/PrecipRate_00.00/20260612/file.grib2.gz", 12),)
    assert token == "next-page"


def test_mrms_catalogue_exposes_radar_products_and_aliases():
    catalog = DatasetCatalog((MrmsProvider(),))

    assert catalog.search("MRMS")[0].dataset_id == DATASET_ID
    assert catalog.search("组合反射率")[0].dataset_id == DATASET_ID
    assert catalog.search(domain="radar")[0].dataset_id == DATASET_ID


@pytest.mark.asyncio
async def test_mrms_download_selects_nearest_time_and_resumes(tmp_path):
    key = "CONUS/PrecipRate_00.00/20260612/MRMS_PrecipRate_00.00_20260612-000400.grib2.gz"
    other = key.replace("000400", "001500")
    prefixes: list[str] = []
    ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            prefixes.append(request.url.params["prefix"])
            return httpx.Response(200, text=listing(((key, 8), (other, 8))))
        ranges.append(request.headers.get("range", ""))
        return httpx.Response(206, content=b"defgh")

    provider = MrmsProvider(
        base_url="https://mrms.test",
        transport=httpx.MockTransport(handler),
    )
    part = tmp_path / "noaa-mrms-pds" / f"{key}.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"abc")

    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=DATASET_ID,
            start_date="2026-06-12",
            end_date="2026-06-12",
            output_dir=tmp_path,
            variables=("precip_rate",),
            times=("00:00",),
        )
    )

    assert prefixes == ["CONUS/PrecipRate_00.00/20260612/"]
    assert ranges == ["bytes=3-"]
    assert result.source_urls == (f"https://mrms.test/{key}",)
    assert Path(result.files[0]).read_bytes() == b"abcdefgh"
    assert result.metadata["products"] == ["PrecipRate_00.00"]
    assert result.metadata["regions"] == ["CONUS"]
    assert result.metadata["requested_times"] == ["0000"]
    assert result.metadata["actual_times"] == ["000400"]


@pytest.mark.asyncio
async def test_mrms_listing_follows_pagination(tmp_path):
    key = (
        "CONUS/MergedReflectivityQCComposite_00.50/20260612/"
        "MRMS_MergedReflectivityQCComposite_00.50_20260612-000442.grib2.gz"
    )
    other = key.replace("000442", "001442")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/":
            calls += 1
            if request.url.params.get("continuation-token"):
                return httpx.Response(200, text=listing(((key, 4),)))
            return httpx.Response(200, text=listing(((other, 4),), "page-2"))
        return httpx.Response(200, content=b"mrms")

    provider = MrmsProvider(
        base_url="https://mrms.test",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=DATASET_ID,
            start_date="2026-06-12",
            end_date="2026-06-12",
            output_dir=tmp_path,
            variables=("reflectivity",),
            times=("00:04",),
            area=(40, -105, 35, -95),
        )
    )

    assert calls == 2
    assert len(result.files) == 1
    assert result.metadata["requires_local_subset"] is True
    assert result.warnings


@pytest.mark.asyncio
async def test_mrms_requires_product_region_and_time(tmp_path):
    provider = MrmsProvider(
        base_url="https://mrms.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=listing(()))),
    )
    base = dict(
        dataset_id=DATASET_ID,
        start_date="2026-06-12",
        end_date="2026-06-12",
        output_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="必须指定变量或产品"):
        await provider.download(DatasetDownloadRequest(**base))
    with pytest.raises(ValueError, match="必须指定 UTC 时次"):
        await provider.download(DatasetDownloadRequest(**base, variables=("reflectivity",)))
    with pytest.raises(ValueError, match="不支持区域"):
        await provider.download(
            DatasetDownloadRequest(
                **base,
                variables=("reflectivity",),
                times=("00:04",),
                platforms=("EUROPE",),
            )
        )
    with pytest.raises(ValueError, match="未找到请求产品"):
        await provider.download(
            DatasetDownloadRequest(
                **base,
                variables=("reflectivity",),
                times=("00:04",),
            )
        )


@pytest.mark.asyncio
async def test_unified_download_tool_passes_mrms_request_fields(monkeypatch, tmp_path):
    from aero.toolbox import builtin_tools

    key = "HAWAII/MESH_00.50/20260612/MRMS_MESH_00.50_20260612-030000.grib2.gz"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text=listing(((key, 4),)))
        return httpx.Response(200, content=b"mesh")

    provider = MrmsProvider(
        base_url="https://mrms.test",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr("aero.datasets.catalog._DEFAULT_CATALOG", DatasetCatalog((provider,)))

    result = await builtin_tools.download_dataset(
        DATASET_ID,
        "2026-06-12",
        "2026-06-12",
        product="MESH_00.50",
        times=["03:00"],
        platforms=["HAWAII"],
        output_dir=str(tmp_path),
    )

    assert result["status"] == "success"
    assert result["metadata"]["products"] == ["MESH_00.50"]
    assert result["metadata"]["regions"] == ["HAWAII"]


@pytest.mark.asyncio
async def test_mrms_missing_product_returns_structured_query_guidance(monkeypatch, tmp_path):
    from aero.toolbox import builtin_tools

    provider = MrmsProvider(base_url="https://mrms.test")
    monkeypatch.setattr("aero.datasets.catalog._DEFAULT_CATALOG", DatasetCatalog((provider,)))

    result = await builtin_tools.download_dataset(
        DATASET_ID,
        "2026-06-12",
        "2026-06-12",
        times=["03:00"],
        output_dir=str(tmp_path),
    )

    assert result["status"] == "error"
    assert result["suggested_tool"] == "search_dataset_variables"
    assert result["suggested_args"] == {"dataset_id": DATASET_ID, "query": ""}
