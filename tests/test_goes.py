"""Tests for NOAA NODD GOES-R ABI provider."""

from pathlib import Path

import httpx
import pytest

from aero.datasets.catalog import DatasetCatalog
from aero.datasets.models import DatasetDownloadRequest
from aero.datasets.providers.goes import (
    L1B_DATASET_ID,
    L2_CMIP_DATASET_ID,
    GoesProvider,
    parse_s3_listing,
)


def listing(objects: tuple[tuple[str, int], ...], token: str = "") -> str:
    contents = "".join(
        f"<Contents><Key>{key}</Key><Size>{size}</Size></Contents>" for key, size in objects
    )
    next_token = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    return f"<ListBucketResult>{contents}{next_token}</ListBucketResult>"


def test_parse_goes_s3_listing_reads_objects_and_continuation_token():
    objects, token = parse_s3_listing(
        listing((("ABI-L1b-RadF/file.nc", 12),), "next-page"),
        "noaa-goes19",
    )

    assert objects[0].key == "ABI-L1b-RadF/file.nc"
    assert objects[0].size == 12
    assert token == "next-page"


def test_goes_catalogue_exposes_full_disk_products_and_channels():
    catalog = DatasetCatalog((GoesProvider(),))

    assert {item.dataset_id for item in catalog.search("GOES")} == {
        L1B_DATASET_ID,
        L2_CMIP_DATASET_ID,
    }
    assert catalog.search("C13")[0].dataset_id == L1B_DATASET_ID
    assert len(catalog.search(domain="satellite")) == 2


@pytest.mark.asyncio
async def test_goes_download_uses_julian_day_filters_time_and_resumes(tmp_path):
    g18_key = (
        "ABI-L1b-RadF/2026/163/03/"
        "OR_ABI-L1b-RadF-M6C13_G18_s20261630300219_e20261630309528_c20261630309584.nc"
    )
    g19_key = g18_key.replace("G18", "G19")
    other_time = g19_key.replace("30300219", "30310219")
    other_channel = g19_key.replace("M6C13", "M6C14")
    content = {g18_key: b"abcdefgh", g19_key: b"12345678"}
    prefixes: list[str] = []
    ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bucket = request.url.host.split(".")[0]
        if request.url.path == "/":
            prefixes.append(request.url.params["prefix"])
            if bucket == "g18":
                return httpx.Response(200, text=listing(((g18_key, 8),)))
            return httpx.Response(
                200,
                text=listing(((g19_key, 8), (other_time, 8), (other_channel, 8))),
            )
        key = request.url.path.lstrip("/")
        range_header = request.headers.get("range", "")
        ranges.append(range_header)
        offset = int(range_header.removeprefix("bytes=").removesuffix("-")) if range_header else 0
        return httpx.Response(206 if offset else 200, content=content[key][offset:])

    provider = GoesProvider(
        bucket_urls={"noaa-goes18": "https://g18.test", "noaa-goes19": "https://g19.test"},
        transport=httpx.MockTransport(handler),
    )
    part = tmp_path / "noaa-goes18" / f"{g18_key}.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(content[g18_key][:3])
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=L1B_DATASET_ID,
            start_date="2026-06-12",
            end_date="2026-06-12",
            output_dir=tmp_path,
            variables=("channel13",),
            times=("03:00",),
            platforms=("GOES-18",),
        )
    )

    assert prefixes == ["ABI-L1b-RadF/2026/163/03/"]
    assert len(result.files) == 1
    assert ranges == ["bytes=3-"]
    assert Path(result.files[0]).read_bytes() in content.values()
    assert result.metadata["channels"] == ["C13"]
    assert result.metadata["times"] == ["0300"]
    assert result.metadata["requested_platforms"] == ["goes18"]
    assert result.metadata["satellites"] == ["goes18"]


@pytest.mark.asyncio
async def test_goes_listing_follows_pagination_for_cmip(tmp_path):
    cmip = (
        "ABI-L2-CMIPF/2026/163/03/"
        "OR_ABI-L2-CMIPF-M6C13_G19_s20261630300219_e20261630309528_c20261630309584.nc"
    )
    other = cmip.replace("M6C13", "M6C14")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/":
            calls += 1
            if request.url.params.get("continuation-token"):
                return httpx.Response(200, text=listing(((cmip, 4),)))
            return httpx.Response(200, text=listing(((other, 5),), "page-2"))
        return httpx.Response(200, content=b"cmip")

    provider = GoesProvider(
        bucket_urls={"noaa-goes19": "https://g19.test"},
        transport=httpx.MockTransport(handler),
    )
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=L2_CMIP_DATASET_ID,
            start_date="2026-06-12",
            end_date="2026-06-12",
            output_dir=tmp_path,
            variables=("C13",),
            times=("0300",),
            area=(40, -130, 20, -100),
        )
    )

    assert calls == 2
    assert len(result.files) == 1
    assert result.metadata["requires_local_subset"] is True
    assert result.warnings


@pytest.mark.asyncio
async def test_goes_requires_channels_and_times(tmp_path):
    provider = GoesProvider(
        bucket_urls={"noaa-goes19": "https://g19.test"},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=listing(()))),
    )
    base = dict(
        dataset_id=L1B_DATASET_ID,
        start_date="2026-06-12",
        end_date="2026-06-12",
        output_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="必须指定变量"):
        await provider.download(DatasetDownloadRequest(**base))
    with pytest.raises(ValueError, match="必须指定 UTC 时次"):
        await provider.download(DatasetDownloadRequest(**base, variables=("C13",)))
    with pytest.raises(ValueError, match="HH:MM"):
        await provider.download(
            DatasetDownloadRequest(**base, variables=("C13",), times=("25:00",))
        )
    with pytest.raises(ValueError, match="不支持卫星"):
        await provider.download(
            DatasetDownloadRequest(
                **base,
                variables=("C13",),
                times=("03:00",),
                platforms=("GOES-20",),
            )
        )


@pytest.mark.asyncio
async def test_unified_download_tool_passes_times_to_goes_provider(monkeypatch, tmp_path):
    from aero.toolbox import builtin_tools

    key = (
        "ABI-L1b-RadF/2026/163/03/"
        "OR_ABI-L1b-RadF-M6C13_G19_s20261630300219_e20261630309528_c20261630309584.nc"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text=listing(((key, 4),)))
        return httpx.Response(200, content=b"goes")

    provider = GoesProvider(
        bucket_urls={"noaa-goes19": "https://g19.test"},
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr("aero.datasets.catalog._DEFAULT_CATALOG", DatasetCatalog((provider,)))

    result = await builtin_tools.download_dataset(
        L1B_DATASET_ID,
        "2026-06-12",
        "2026-06-12",
        variables=["C13"],
        times=["03:00"],
        output_dir=str(tmp_path),
    )

    assert result["status"] == "success"
    assert result["metadata"]["times"] == ["0300"]


@pytest.mark.asyncio
async def test_goes_missing_channel_returns_structured_query_guidance(monkeypatch, tmp_path):
    from aero.toolbox import builtin_tools

    provider = GoesProvider(bucket_urls={"noaa-goes19": "https://g19.test"})
    monkeypatch.setattr("aero.datasets.catalog._DEFAULT_CATALOG", DatasetCatalog((provider,)))

    result = await builtin_tools.download_dataset(
        L1B_DATASET_ID,
        "2026-06-12",
        "2026-06-12",
        times=["03:00"],
        platforms=["GOES-19"],
        output_dir=str(tmp_path),
    )

    assert result["status"] == "error"
    assert result["suggested_tool"] == "search_dataset_variables"
    assert result["suggested_args"] == {"dataset_id": L1B_DATASET_ID, "query": ""}
