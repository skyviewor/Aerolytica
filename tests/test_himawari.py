"""Tests for NOAA NODD Himawari provider."""

from pathlib import Path

import httpx
import pytest

from aero.datasets.catalog import DatasetCatalog
from aero.datasets.models import DatasetDownloadRequest
from aero.datasets.providers.himawari import (
    L1B_DATASET_ID,
    L2_CLOUDS_DATASET_ID,
    HimawariProvider,
    parse_s3_listing,
)


def listing(objects: tuple[tuple[str, int], ...], token: str = "") -> str:
    contents = "".join(
        f"<Contents><Key>{key}</Key><Size>{size}</Size></Contents>" for key, size in objects
    )
    next_token = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    return f"<ListBucketResult>{contents}{next_token}</ListBucketResult>"


def test_parse_s3_listing_reads_objects_and_continuation_token():
    objects, token = parse_s3_listing(
        listing((("AHI-L1b-FLDK/file.DAT.bz2", 12),), "next-page"),
        "noaa-himawari9",
    )

    assert objects[0].key == "AHI-L1b-FLDK/file.DAT.bz2"
    assert objects[0].size == 12
    assert token == "next-page"


def test_himawari_catalogue_exposes_satellite_products_and_variables():
    catalog = DatasetCatalog((HimawariProvider(),))

    assert {item.dataset_id for item in catalog.search("葵花")} == {
        L1B_DATASET_ID,
        L2_CLOUDS_DATASET_ID,
    }
    assert catalog.search("B13")[0].dataset_id == L1B_DATASET_ID
    assert catalog.search("云掩膜")[0].dataset_id == L2_CLOUDS_DATASET_ID
    assert len(catalog.search(domain="satellite")) == 2


@pytest.mark.asyncio
async def test_himawari_download_filters_band_across_buckets_and_resumes(tmp_path):
    h8_key = "AHI-L1b-FLDK/2022/12/12/0000/HS_H08_20221212_0000_B13_FLDK_R20_S0110.DAT.bz2"
    h9_key = "AHI-L1b-FLDK/2022/12/12/0000/HS_H09_20221212_0000_B13_FLDK_R20_S0110.DAT.bz2"
    other_key = "AHI-L1b-FLDK/2022/12/12/0000/HS_H09_20221212_0000_B14_FLDK_R20_S0110.DAT.bz2"
    content = {h8_key: b"abcdefgh", h9_key: b"12345678"}
    ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bucket = request.url.host.split(".")[0]
        if request.url.path == "/":
            if bucket == "h8":
                return httpx.Response(200, text=listing(((h8_key, 8),)))
            return httpx.Response(200, text=listing(((h9_key, 8), (other_key, 8))))
        key = request.url.path.lstrip("/")
        range_header = request.headers.get("range", "")
        ranges.append(range_header)
        offset = int(range_header.removeprefix("bytes=").removesuffix("-")) if range_header else 0
        return httpx.Response(206 if offset else 200, content=content[key][offset:])

    provider = HimawariProvider(
        bucket_urls={"noaa-himawari8": "https://h8.test", "noaa-himawari9": "https://h9.test"},
        transport=httpx.MockTransport(handler),
    )
    part = tmp_path / "noaa-himawari8" / f"{h8_key}.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(content[h8_key][:3])
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=L1B_DATASET_ID,
            start_date="2022-12-12",
            end_date="2022-12-12",
            output_dir=tmp_path,
            variables=("band13",),
        )
    )

    assert len(result.files) == 2
    assert ranges == ["bytes=3-", ""]
    assert Path(result.files[0]).read_bytes() in content.values()
    assert result.metadata["variables"] == ["B13"]
    assert result.metadata["satellites"] == ["himawari8", "himawari9"]


@pytest.mark.asyncio
async def test_himawari_listing_follows_pagination_and_filters_cloud_product(tmp_path):
    cloud_mask = (
        "AHI-L2-FLDK-Clouds/2026/06/11/0000/"
        "AHI-CMSK_v1r1_h09_s202606110000211_e202606110009405_c202606110017453.nc"
    )
    cloud_phase = cloud_mask.replace("CMSK", "CPHS")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/":
            calls += 1
            if request.url.params.get("continuation-token"):
                return httpx.Response(200, text=listing(((cloud_mask, 4),)))
            return httpx.Response(200, text=listing(((cloud_phase, 5),), "page-2"))
        return httpx.Response(200, content=b"mask")

    provider = HimawariProvider(
        bucket_urls={"noaa-himawari9": "https://h9.test"},
        transport=httpx.MockTransport(handler),
    )
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=L2_CLOUDS_DATASET_ID,
            start_date="2026-06-11",
            end_date="2026-06-11",
            output_dir=tmp_path,
            variables=("云掩膜",),
            area=(40, 100, 20, 120),
        )
    )

    assert calls == 2
    assert len(result.files) == 1
    assert result.metadata["requires_local_subset"] is True
    assert result.warnings


@pytest.mark.asyncio
async def test_himawari_requires_variables_and_reports_missing_dates(tmp_path):
    provider = HimawariProvider(
        bucket_urls={"noaa-himawari9": "https://h9.test"},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=listing(()))),
    )
    with pytest.raises(ValueError, match="必须指定变量"):
        await provider.download(
            DatasetDownloadRequest(
                dataset_id=L1B_DATASET_ID,
                start_date="2026-06-11",
                end_date="2026-06-11",
                output_dir=tmp_path,
            )
        )
    with pytest.raises(ValueError, match="2026-06-11"):
        await provider.download(
            DatasetDownloadRequest(
                dataset_id=L1B_DATASET_ID,
                start_date="2026-06-11",
                end_date="2026-06-11",
                output_dir=tmp_path,
                variables=("B13",),
            )
        )


@pytest.mark.asyncio
async def test_himawari_missing_variable_returns_structured_query_guidance(monkeypatch, tmp_path):
    from aero.toolbox import builtin_tools

    monkeypatch.setattr(
        "aero.datasets.catalog._DEFAULT_CATALOG",
        DatasetCatalog((HimawariProvider(bucket_urls={"noaa-himawari9": "https://h9.test"}),)),
    )
    result = await builtin_tools.download_dataset(
        L1B_DATASET_ID,
        "2026-06-11",
        "2026-06-11",
        output_dir=str(tmp_path),
    )

    assert result["status"] == "error"
    assert result["suggested_tool"] == "search_dataset_variables"
    assert result["suggested_args"] == {"dataset_id": L1B_DATASET_ID, "query": ""}
