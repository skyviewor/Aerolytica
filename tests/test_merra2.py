"""Tests for NASA GES DISC MERRA-2 provider."""

import httpx
import pytest

from aero.datasets.models import DatasetDownloadRequest
from aero.datasets.providers.merra2 import MERRA2_SPECS, Merra2Provider, _download_link


def test_merra2_specs_cover_common_products():
    assert {spec.dataset_id for spec in MERRA2_SPECS} == {
        "merra2-single-level-hourly",
        "merra2-single-level-instant-hourly",
        "merra2-pressure-3hourly",
        "merra2-single-level-monthly",
        "merra2-pressure-monthly",
    }
    assert all(spec.provider_id == "nasa-gesdisc" for spec in MERRA2_SPECS)
    assert all(spec.requires_auth for spec in MERRA2_SPECS)


def test_download_link_prefers_https_data_nc4_link():
    entry = {
        "links": [
            {"rel": "http://esipfed.org/ns/fedsearch/1.1/s3#", "href": "s3://bucket/file.nc4"},
            {
                "rel": "http://esipfed.org/ns/fedsearch/1.1/data#",
                "href": "https://data.gesdisc.earthdata.nasa.gov/data/file.nc4",
            },
        ]
    }

    assert _download_link(entry) == "https://data.gesdisc.earthdata.nasa.gov/data/file.nc4"


@pytest.mark.asyncio
async def test_search_variables_filters_common_merra2_variables():
    provider = Merra2Provider()

    variables = await provider.search_variables("merra2-pressure-3hourly", "wind")

    assert "U: eastward wind (m/s)" in variables
    assert "V: northward wind (m/s)" in variables


@pytest.mark.asyncio
async def test_download_uses_cmr_granule_links_and_reports_subset_warning(tmp_path):
    content = b"netcdf"
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.url.host == "cmr.test":
            return httpx.Response(
                200,
                json={
                    "feed": {
                        "entry": [
                            {
                                "id": "G1",
                                "producer_granule_id": "MERRA2_400.tavg1.nc4",
                                "links": [
                                    {
                                        "rel": "http://esipfed.org/ns/fedsearch/1.1/data#",
                                        "href": "https://data.test/MERRA2_400.tavg1.nc4",
                                    }
                                ],
                            }
                        ]
                    }
                },
            )
        if request.url.host == "data.test":
            return httpx.Response(
                200,
                content=content,
                headers={"content-length": str(len(content))},
            )
        raise AssertionError(f"unexpected URL: {request.url}")

    provider = Merra2Provider(
        cmr_url="https://cmr.test/search/granules.json",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id="merra2-single-level-hourly",
            start_date="2020-01-01",
            end_date="2020-01-01",
            output_dir=tmp_path,
            variables=("T2M",),
            area=(40, 100, 20, 120),
        )
    )

    assert requests[0][0] == "GET"
    assert "collection_concept_id=C1276812863-GES_DISC" in requests[0][1]
    assert result.files[0].read_bytes() == content
    assert result.source_urls == ("https://data.test/MERRA2_400.tavg1.nc4",)
    assert result.warnings
    assert result.metadata["short_name"] == "M2T1NXSLV"


@pytest.mark.asyncio
async def test_download_uses_saved_earthdata_token(tmp_path, monkeypatch):
    from aero.core.config import save_earthdata_token

    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))
    save_earthdata_token("earthdata-token-0002")
    auth_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cmr.test":
            return httpx.Response(
                200,
                json={
                    "feed": {
                        "entry": [
                            {
                                "id": "G1",
                                "links": [
                                    {
                                        "rel": "http://esipfed.org/ns/fedsearch/1.1/data#",
                                        "href": "https://data.test/file.nc4",
                                    }
                                ],
                            }
                        ]
                    }
                },
            )
        auth_headers.append(request.headers.get("authorization", ""))
        return httpx.Response(200, content=b"ok", headers={"content-length": "2"})

    provider = Merra2Provider(
        cmr_url="https://cmr.test/search/granules.json",
        transport=httpx.MockTransport(handler),
    )
    await provider.download(
        DatasetDownloadRequest(
            dataset_id="merra2-single-level-hourly",
            start_date="2020-01-01",
            end_date="2020-01-01",
            output_dir=tmp_path,
        )
    )

    assert auth_headers == ["Bearer earthdata-token-0002"]


@pytest.mark.asyncio
async def test_download_reports_earthdata_auth_guidance(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cmr.test":
            return httpx.Response(
                200,
                json={
                    "feed": {
                        "entry": [
                            {
                                "id": "G1",
                                "links": [
                                    {
                                        "rel": "http://esipfed.org/ns/fedsearch/1.1/data#",
                                        "href": "https://data.test/file.nc4",
                                    }
                                ],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(401)

    provider = Merra2Provider(
        cmr_url="https://cmr.test/search/granules.json",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="Earthdata Login"):
        await provider.download(
            DatasetDownloadRequest(
                dataset_id="merra2-single-level-hourly",
                start_date="2020-01-01",
                end_date="2020-01-01",
                output_dir=tmp_path,
            )
        )
