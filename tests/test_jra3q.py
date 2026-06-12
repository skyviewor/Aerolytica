"""Tests for NCAR GDEX JRA-3Q provider."""

from pathlib import Path

import httpx
import pytest

from aero.datasets.catalog import DatasetCatalog
from aero.datasets.models import DatasetDownloadRequest
from aero.datasets.providers.jra3q import (
    PRESSURE_DATASET_ID,
    SURFACE_DATASET_ID,
    Jra3qProvider,
    parse_catalog_xml,
)

CATALOG_XML = """<?xml version="1.0"?>
<catalog xmlns="http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0">
  <dataset name="jra3q.anl_p125.0_0_0.tmp-pres-an-ll125.2025010100_2025013118.nc"
    urlPath="files/g/d640000/anl_p125/202501/jra3q.anl_p125.0_0_0.tmp-pres-an-ll125.2025010100_2025013118.nc"/>
  <dataset name="jra3q.anl_p125.0_2_2.ugrd-pres-an-ll125.2025010100_2025013118.nc"
    urlPath="files/g/d640000/anl_p125/202501/jra3q.anl_p125.0_2_2.ugrd-pres-an-ll125.2025010100_2025013118.nc"/>
</catalog>
"""


def test_jra3q_catalog_parser_reads_monthly_variable_entries():
    entries = parse_catalog_xml(CATALOG_XML)

    assert len(entries) == 2
    assert entries[0].collection == "anl_p125"
    assert entries[0].variable == "tmp"
    assert entries[0].code == "0_0_0"
    assert entries[0].grid_name == "tmp-pres-an-ll125"
    assert entries[0].start.isoformat() == "2025-01-01T00:00:00"
    assert entries[0].end.isoformat() == "2025-01-31T18:00:00"


def test_jra3q_catalogue_exposes_pressure_and_surface_products():
    catalog = DatasetCatalog((Jra3qProvider(),))

    assert {item.dataset_id for item in catalog.search("JRA-3Q")} == {
        PRESSURE_DATASET_ID,
        SURFACE_DATASET_ID,
    }
    assert catalog.search("位势高度")[0].dataset_id == PRESSURE_DATASET_ID
    assert catalog.search("海平面气压")[0].dataset_id == SURFACE_DATASET_ID


@pytest.mark.asyncio
async def test_jra3q_download_uses_month_catalog_and_grid_name(tmp_path):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("/catalog.xml"):
            return httpx.Response(200, text=CATALOG_XML)
        return httpx.Response(200, content=b"netcdf")

    provider = Jra3qProvider(
        catalog_base="https://jra.test/thredds/catalog/files/g/d640000",
        ncss_base="https://jra.test/thredds/ncss/grid",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=PRESSURE_DATASET_ID,
            start_date="2025-01-01",
            end_date="2025-01-02",
            output_dir=tmp_path,
            variables=("temperature",),
            levels=(500,),
            area=(40, 110, 35, 120),
        )
    )

    assert Path(result.files[0]).read_bytes() == b"netcdf"
    assert result.metadata["variables"] == ["tmp"]
    assert result.metadata["levels"] == [500]
    assert any("/anl_p125/202501/catalog.xml" in url for url in requests)
    assert any("var=tmp-pres-an-ll125" in url for url in requests)
    assert any("vertCoord=500" in url for url in requests)


@pytest.mark.asyncio
async def test_jra3q_search_variables_uses_sample_month_catalog():
    provider = Jra3qProvider(
        catalog_base="https://jra.test/thredds/catalog/files/g/d640000",
        ncss_base="https://jra.test/thredds/ncss/grid",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=CATALOG_XML)),
    )

    variables = await provider.search_variables(PRESSURE_DATASET_ID, "temperature")

    assert variables == ("tmp: temperature (K)",)


@pytest.mark.asyncio
async def test_jra3q_validates_variables_surface_levels_and_unpublished_month(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    provider = Jra3qProvider(
        catalog_base="https://jra.test/thredds/catalog/files/g/d640000",
        ncss_base="https://jra.test/thredds/ncss/grid",
        transport=httpx.MockTransport(handler),
    )
    base = dict(
        start_date="2025-01-01",
        end_date="2025-01-01",
        output_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="必须指定变量"):
        await provider.download(DatasetDownloadRequest(dataset_id=PRESSURE_DATASET_ID, **base))
    with pytest.raises(ValueError, match="地表产品不支持 levels"):
        await provider.download(
            DatasetDownloadRequest(
                dataset_id=SURFACE_DATASET_ID,
                variables=("pres",),
                levels=(500,),
                **base,
            )
        )
    with pytest.raises(ValueError, match="尚未发布"):
        await provider.download(
            DatasetDownloadRequest(
                dataset_id=PRESSURE_DATASET_ID,
                variables=("tmp",),
                **base,
            )
        )


@pytest.mark.asyncio
async def test_unified_download_tool_passes_jra3q_fields(monkeypatch, tmp_path):
    from aero.toolbox import builtin_tools

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/catalog.xml"):
            return httpx.Response(200, text=CATALOG_XML)
        return httpx.Response(200, content=b"netcdf")

    provider = Jra3qProvider(
        catalog_base="https://jra.test/thredds/catalog/files/g/d640000",
        ncss_base="https://jra.test/thredds/ncss/grid",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr("aero.datasets.catalog._DEFAULT_CATALOG", DatasetCatalog((provider,)))

    result = await builtin_tools.download_dataset(
        PRESSURE_DATASET_ID,
        "2025-01-01",
        "2025-01-02",
        variables=["tmp"],
        levels=[500],
        area=[40, 110, 35, 120],
        output_dir=str(tmp_path),
    )

    assert result["status"] == "success"
    assert result["metadata"]["variables"] == ["tmp"]
