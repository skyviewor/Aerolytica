"""Tests for NCAR GDEX JRA-55 provider."""

from pathlib import Path

import httpx
import pytest

from aero.datasets.catalog import DatasetCatalog
from aero.datasets.models import DatasetDownloadRequest
from aero.datasets.providers.jra55 import (
    PRESSURE_DATASET_ID,
    SURFACE_DATASET_ID,
    Jra55Provider,
    parse_catalog_xml,
)

CATALOG_XML = """<?xml version="1.0"?>
<catalog xmlns="http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0">
  <dataset name="anl_p125.011_tmp.2020010100_2020013118"
    urlPath="files/g/d628000/anl_p125/2020/anl_p125.011_tmp.2020010100_2020013118"/>
  <dataset name="anl_p125.033_ugrd.2020020100_2020022918"
    urlPath="files/g/d628000/anl_p125/2020/anl_p125.033_ugrd.2020020100_2020022918"/>
</catalog>
"""

DATASET_XML = """<?xml version="1.0"?>
<gridDataset>
  <gridSet>
    <grid name="Temperature_isobaric_surface_low" desc="Temperature @ Isobaric Surface"/>
  </gridSet>
</gridDataset>
"""


def test_jra55_catalog_parser_reads_monthly_variable_entries():
    entries = parse_catalog_xml(CATALOG_XML)

    assert len(entries) == 2
    assert entries[0].collection == "anl_p125"
    assert entries[0].variable == "tmp"
    assert entries[0].code == "011"
    assert entries[0].start.isoformat() == "2020-01-01T00:00:00"
    assert entries[0].end.isoformat() == "2020-01-31T18:00:00"


def test_jra55_catalogue_exposes_pressure_and_surface_products():
    catalog = DatasetCatalog((Jra55Provider(),))

    assert {item.dataset_id for item in catalog.search("JRA-55")} == {
        PRESSURE_DATASET_ID,
        SURFACE_DATASET_ID,
    }
    assert catalog.search("位势高度")[0].dataset_id == PRESSURE_DATASET_ID
    assert catalog.search("海平面气压")[0].dataset_id == SURFACE_DATASET_ID
    assert catalog.describe(PRESSURE_DATASET_ID).temporal_coverage.endswith("2024-01-31 18:00 UTC")


@pytest.mark.asyncio
async def test_jra55_download_uses_catalog_and_ncss_grid_name(tmp_path):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("/catalog.xml"):
            return httpx.Response(200, text=CATALOG_XML)
        if request.url.path.endswith("/dataset.xml"):
            return httpx.Response(200, text=DATASET_XML)
        return httpx.Response(200, content=b"netcdf")

    provider = Jra55Provider(
        catalog_base="https://jra.test/thredds/catalog/files/g/d628000",
        ncss_base="https://jra.test/thredds/ncss/grid",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.download(
        DatasetDownloadRequest(
            dataset_id=PRESSURE_DATASET_ID,
            start_date="2020-01-01",
            end_date="2020-01-02",
            output_dir=tmp_path,
            variables=("temperature",),
            levels=(500,),
            area=(40, 110, 35, 120),
        )
    )

    assert Path(result.files[0]).read_bytes() == b"netcdf"
    assert result.metadata["variables"] == ["tmp"]
    assert result.metadata["levels"] == [500]
    assert any("var=Temperature_isobaric_surface_low" in url for url in requests)
    assert any("vertCoord=500" in url for url in requests)
    assert result.files[0].name.endswith("_500hPa_subset.nc")


@pytest.mark.asyncio
async def test_jra55_search_variables_uses_year_catalog(tmp_path):
    provider = Jra55Provider(
        catalog_base="https://jra.test/thredds/catalog/files/g/d628000",
        ncss_base="https://jra.test/thredds/ncss/grid",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=CATALOG_XML)),
    )

    variables = await provider.search_variables(PRESSURE_DATASET_ID, "temperature")

    assert variables == ("tmp: temperature (K)",)


@pytest.mark.asyncio
async def test_jra55_validates_variables_and_surface_levels(tmp_path):
    provider = Jra55Provider(
        catalog_base="https://jra.test/thredds/catalog/files/g/d628000",
        ncss_base="https://jra.test/thredds/ncss/grid",
    )
    base = dict(
        start_date="2020-01-01",
        end_date="2020-01-01",
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
    with pytest.raises(ValueError, match="2024-01-31"):
        await provider.download(
            DatasetDownloadRequest(
                dataset_id=PRESSURE_DATASET_ID,
                start_date="2024-02-01",
                end_date="2024-02-01",
                output_dir=tmp_path,
                variables=("tmp",),
            )
        )


@pytest.mark.asyncio
async def test_unified_download_tool_passes_jra55_fields(monkeypatch, tmp_path):
    from aero.toolbox import builtin_tools

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/catalog.xml"):
            return httpx.Response(200, text=CATALOG_XML)
        if request.url.path.endswith("/dataset.xml"):
            return httpx.Response(200, text=DATASET_XML)
        return httpx.Response(200, content=b"netcdf")

    provider = Jra55Provider(
        catalog_base="https://jra.test/thredds/catalog/files/g/d628000",
        ncss_base="https://jra.test/thredds/ncss/grid",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr("aero.datasets.catalog._DEFAULT_CATALOG", DatasetCatalog((provider,)))

    result = await builtin_tools.download_dataset(
        PRESSURE_DATASET_ID,
        "2020-01-01",
        "2020-01-02",
        variables=["tmp"],
        levels=[500],
        area=[40, 110, 35, 120],
        output_dir=str(tmp_path),
    )

    assert result["status"] == "success"
    assert result["metadata"]["variables"] == ["tmp"]
