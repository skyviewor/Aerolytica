"""Tests for CAMS ADS tools."""

from datetime import date, datetime, timezone

import httpx
import pytest

from aero.toolbox.tools.cams import (
    EAC4_DATASET_ID,
    FORECAST_DATASET_ID,
    _build_cams_request,
    _dest_path,
    download_cams,
    get_cams_latest_forecast_cycle,
    search_cams_variables,
)


@pytest.fixture(autouse=True)
def fake_cams_variable_catalogue(monkeypatch):
    async def fake_get_cams_variables(dataset_id=None):
        records = [
            {
                "dataset_id": dataset_id or EAC4_DATASET_ID,
                "name": "total_column_ozone",
                "label": "Total column ozone",
                "level_type": "single",
                "group": "Single level",
            },
            {
                "dataset_id": dataset_id or EAC4_DATASET_ID,
                "name": "ozone",
                "label": "Ozone",
                "level_type": "multi",
                "group": "Multi level",
            },
            {
                "dataset_id": dataset_id or EAC4_DATASET_ID,
                "name": "particulate_matter_2.5um",
                "label": "Particulate matter d < 2.5 um (PM2.5)",
                "level_type": "single",
                "group": "Single level",
            },
        ]
        return records

    monkeypatch.setattr(
        "aero.data.cams_variables.get_cams_variables",
        fake_get_cams_variables,
    )


def test_build_eac4_request_uses_ads_date_range_and_netcdf_zip():
    request = _build_cams_request(
        dataset_id=EAC4_DATASET_ID,
        variables=["total_column_ozone"],
        start=date(2025, 1, 1),
        end=date(2025, 1, 2),
        times=["00:00", "12:00"],
        leadtime_hours=(),
        pressure_levels=(),
        area=[60, 70, 10, 140],
        data_format="netcdf",
    )

    assert request == {
        "variable": ["total_column_ozone"],
        "date": "2025-01-01/2025-01-02",
        "time": ["00:00", "12:00"],
        "data_format": "netcdf_zip",
        "area": [60, 70, 10, 140],
    }


def test_build_forecast_request_adds_type_and_leadtime():
    request = _build_cams_request(
        dataset_id=FORECAST_DATASET_ID,
        variables=["particulate_matter_2.5um"],
        start=date(2025, 1, 1),
        end=date(2025, 1, 1),
        times=["00:00"],
        leadtime_hours=(0, 24, 48),
        pressure_levels=(850, 500),
        area=None,
        data_format="grib",
    )

    assert request["type"] == ["forecast"]
    assert request["leadtime_hour"] == ["0", "24", "48"]
    assert request["pressure_level"] == ["850", "500"]
    assert request["data_format"] == "grib"


def test_latest_guaranteed_cams_cycle_before_00z_publication_is_previous_12z():
    from aero.data.cams_availability import latest_guaranteed_cams_forecast_cycle

    result = latest_guaranteed_cams_forecast_cycle(
        datetime(2026, 6, 13, 3, 25, tzinfo=timezone.utc)
    )

    assert result["latest_guaranteed"]["date"] == "2026-06-12"
    assert result["latest_guaranteed"]["cycle"] == "12"
    assert result["newer_not_yet_guaranteed"][0]["date"] == "2026-06-13"
    assert result["newer_not_yet_guaranteed"][0]["cycle"] == "00"


def test_latest_guaranteed_cams_cycle_after_12z_publication_is_current_12z():
    from aero.data.cams_availability import latest_guaranteed_cams_forecast_cycle

    result = latest_guaranteed_cams_forecast_cycle(
        datetime(2026, 6, 13, 23, 0, tzinfo=timezone.utc)
    )

    assert result["latest_guaranteed"]["date"] == "2026-06-13"
    assert result["latest_guaranteed"]["cycle"] == "12"
    assert result["newer_not_yet_guaranteed"] == []


@pytest.mark.asyncio
async def test_get_cams_latest_forecast_cycle_returns_download_args(monkeypatch):
    from aero.data import cams_availability

    async def fake_latest(reference):
        schedule = cams_availability.latest_guaranteed_cams_forecast_cycle(reference)
        return {
            **schedule,
            "latest_available": {
                "date": "2026-06-13",
                "cycle": "00",
                "run_time_utc": "2026-06-13T00:00:00Z",
            },
            "availability_basis": "ads_costing_api",
            "actual_ads_probe_performed": True,
            "checks": [],
        }

    monkeypatch.setattr(cams_availability, "get_latest_available_cams_forecast_cycle", fake_latest)
    result = await get_cams_latest_forecast_cycle("2026-06-13T15:00:00Z")

    assert result["status"] == "success"
    assert result["latest_guaranteed"]["date"] == "2026-06-13"
    assert result["latest_guaranteed"]["cycle"] == "00"
    assert result["recommended_download_args"] == {
        "dataset_id": FORECAST_DATASET_ID,
        "start_date": "2026-06-13",
        "end_date": "2026-06-13",
        "times": ["00:00"],
    }
    assert result["newer_not_yet_guaranteed"][0]["cycle"] == "12"


@pytest.mark.asyncio
async def test_cams_latest_cycle_probe_uses_ads_validation_without_downloading():
    from aero.data.cams_availability import get_latest_available_cams_forecast_cycle

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = request.content.decode()
        if "2026-06-13/2026-06-13" in body:
            return httpx.Response(
                200,
                json={"request_is_valid": False, "invalid_reason": "not available"},
            )
        return httpx.Response(200, json={"id": "size", "cost": 1.0, "limit": 10000.0})

    result = await get_latest_available_cams_forecast_cycle(
        datetime(2026, 6, 13, 3, 25, tzinfo=timezone.utc),
        transport=httpx.MockTransport(handler),
    )

    assert result["latest_available"]["date"] == "2026-06-12"
    assert result["latest_available"]["cycle"] == "12"
    assert result["availability_basis"] == "ads_costing_api"
    assert len(requests) == 2
    assert all(request.url.path.endswith("/costing") for request in requests)


def test_cams_dest_path_sanitizes_filename_parts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    (tmp_path / "aero.yaml").write_text("output:\n  data_dir: data\n")

    path = _dest_path(
        "CAMS 全球大气成分预报数据集",
        ["particulate matter 2.5um"],
        date(2026, 6, 12),
        date(2026, 6, 12),
        "netcdf",
    )

    assert path.name == "cams_CAMS_particulate_matter_2.5um_20260612.nc"
    assert " " not in path.name


@pytest.mark.asyncio
async def test_download_cams_requires_ads_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))

    result = await download_cams(
        variables=["total_column_ozone"],
        start_date="2025-01-01",
        end_date="2025-01-01",
    )

    assert result["status"] == "error"
    assert result["suggested_tool"] == "check_ads_config"
    assert "ADS API key 未配置" in result["message"]


@pytest.mark.asyncio
async def test_download_cams_submits_ads_request_and_fetches_file(tmp_path, monkeypatch):
    from aero.adapters.cds_adapter import CDSAdapter
    from aero.core.config import save_ads_credentials

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    (tmp_path / "aero.yaml").write_text("output:\n  data_dir: data\n")
    save_ads_credentials("https://ads.atmosphere.copernicus.eu/api", "ads-token")

    captured: dict = {}

    async def fake_submit(self, **kwargs):
        captured.update(kwargs)
        return {
            "download_url": "https://example.test/cams.nc",
            "dest_path": kwargs["dest_path"],
            "request_id": "ads-123",
            "total_bytes": 0,
            "accept_ranges": "",
        }

    async def fake_fetch(self, download_url, dest_path, **kwargs):
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"CDF " + b"\0" * 2048)
        return dest_path.stat().st_size

    monkeypatch.setattr(CDSAdapter, "submit", fake_submit)
    monkeypatch.setattr(CDSAdapter, "fetch", fake_fetch)

    result = await download_cams(
        dataset_id=FORECAST_DATASET_ID,
        variables=["particulate_matter_2.5um"],
        start_date="2025-01-01",
        end_date="2025-01-01",
        times=["00:00"],
        leadtime_hours=[0, 24],
    )

    assert result["status"] == "success"
    assert result["request_id"] == "ads-123"
    assert captured["dataset_id"] == FORECAST_DATASET_ID
    assert captured["request_overrides"]["type"] == ["forecast"]
    assert captured["request_overrides"]["leadtime_hour"] == ["0", "24"]
    assert captured["dest_path"].name.startswith("cams_")


@pytest.mark.asyncio
async def test_search_cams_variables_distinguishes_total_column_and_multilevel_ozone():
    result = await search_cams_variables(query="臭氧")

    assert result["status"] == "success"
    assert result["found"] is True
    variables = {item["name"]: item for item in result["variables"]}
    assert variables["total_column_ozone"]["level_type"] == "single"
    assert variables["ozone"]["level_type"] == "multi"


@pytest.mark.asyncio
async def test_download_cams_resolves_common_ads_aliases(tmp_path, monkeypatch):
    from aero.adapters.cds_adapter import CDSAdapter
    from aero.core.config import save_ads_credentials

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    (tmp_path / "aero.yaml").write_text("output:\n  data_dir: data\n")
    save_ads_credentials("https://ads.atmosphere.copernicus.eu/api", "ads-token")

    captured: dict = {}

    async def fake_submit(self, **kwargs):
        captured.update(kwargs)
        return {
            "download_url": "https://example.test/cams.nc",
            "dest_path": kwargs["dest_path"],
            "request_id": "ads-123",
            "total_bytes": 0,
            "accept_ranges": "",
        }

    async def fake_fetch(self, download_url, dest_path, **kwargs):
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"CDF " + b"\0" * 2048)
        return dest_path.stat().st_size

    monkeypatch.setattr(CDSAdapter, "submit", fake_submit)
    monkeypatch.setattr(CDSAdapter, "fetch", fake_fetch)

    result = await download_cams(
        variables=["tco3", "pm25"],
        start_date="2025-01-01",
        end_date="2025-01-01",
    )

    assert result["status"] == "success"
    assert captured["request_overrides"]["variable"] == [
        "total_column_ozone",
        "particulate_matter_2.5um",
    ]
    assert result["variables"] == ["total_column_ozone", "particulate_matter_2.5um"]


@pytest.mark.asyncio
async def test_download_cams_renames_actual_grib_response(tmp_path, monkeypatch):
    from aero.adapters.cds_adapter import CDSAdapter
    from aero.core.config import save_ads_credentials

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    (tmp_path / "aero.yaml").write_text("output:\n  data_dir: data\n")
    save_ads_credentials("https://ads.atmosphere.copernicus.eu/api", "ads-token")

    async def fake_submit(self, **kwargs):
        return {
            "download_url": "https://example.test/cams.grib",
            "dest_path": kwargs["dest_path"],
            "request_id": "ads-456",
            "total_bytes": 0,
            "accept_ranges": "",
        }

    async def fake_fetch(self, download_url, dest_path, **kwargs):
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"GRIB" + b"\0" * 2048)
        return dest_path.stat().st_size

    monkeypatch.setattr(CDSAdapter, "submit", fake_submit)
    monkeypatch.setattr(CDSAdapter, "fetch", fake_fetch)

    result = await download_cams(
        dataset_id=FORECAST_DATASET_ID,
        variables=["particulate_matter_2_5um"],
        start_date="2026-06-12",
        end_date="2026-06-12",
        times=["12:00"],
        leadtime_hours=[24],
        data_format="netcdf",
    )

    assert result["status"] == "success"
    assert result["actual_file_format"] == "grib"
    assert result["file_path"].endswith(".grib")
    assert (tmp_path / result["file_path"]).read_bytes().startswith(b"GRIB")
    assert " " not in (tmp_path / result["file_path"]).name


@pytest.mark.asyncio
async def test_download_cams_rejects_pressure_levels_for_single_level_variable():
    result = await download_cams(
        variables=["total_column_ozone"],
        start_date="2025-01-01",
        end_date="2025-01-01",
        pressure_levels=[500],
    )

    assert result["status"] == "error"
    assert "single level" in result["message"]
    assert "pressure_levels" in result["message"]


@pytest.mark.asyncio
async def test_unified_dataset_variable_search_uses_cams_ads_names():
    from aero.toolbox.tools.datasets import search_dataset_variables

    result = await search_dataset_variables(EAC4_DATASET_ID, query="ozone")

    assert result["status"] == "success"
    assert result["count"] == 2
    assert "total_column_ozone: Total column ozone (single level)" in result["variables"]
    assert "ozone: Ozone (multi level)" in result["variables"]


@pytest.mark.asyncio
async def test_download_cams_submission_error_includes_direct_terms_url(tmp_path, monkeypatch):
    from aero.adapters.cds_adapter import CDSAdapter
    from aero.core.config import save_ads_credentials

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    (tmp_path / "aero.yaml").write_text("output:\n  data_dir: data\n")
    save_ads_credentials("https://ads.atmosphere.copernicus.eu/api", "ads-token")

    async def fake_submit(self, **kwargs):
        raise RuntimeError("Terms must be accepted")

    monkeypatch.setattr(CDSAdapter, "submit", fake_submit)

    result = await download_cams(
        dataset_id=EAC4_DATASET_ID,
        variables=["total_column_ozone"],
        start_date="2025-01-01",
        end_date="2025-01-01",
    )

    assert result["status"] == "error"
    assert result["terms_url"] == (
        "https://ads.atmosphere.copernicus.eu/datasets/"
        "cams-global-reanalysis-eac4?tab=download"
    )
    assert result["terms_url"] in result["message"]


@pytest.mark.asyncio
async def test_download_cams_schema_error_does_not_claim_terms(tmp_path, monkeypatch):
    from aero.adapters.cds_adapter import CDSAdapter
    from aero.core.config import save_ads_credentials

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    (tmp_path / "aero.yaml").write_text("output:\n  data_dir: data\n")
    save_ads_credentials("https://ads.atmosphere.copernicus.eu/api", "ads-token")

    async def fake_submit(self, **kwargs):
        raise RuntimeError(
            "400 Client Error: Bad Request for url: "
            "https://ads.atmosphere.copernicus.eu/api/retrieve/v1/processes/"
            "cams-global-reanalysis-eac4/execution\n"
            "invalid request\n"
            "request: Invalid key name: 'product_type'"
        )

    monkeypatch.setattr(CDSAdapter, "submit", fake_submit)

    result = await download_cams(
        dataset_id=EAC4_DATASET_ID,
        variables=["total_column_ozone"],
        start_date="2025-01-01",
        end_date="2025-01-01",
    )

    assert result["status"] == "error"
    assert "请求参数" in result["message"]
    assert "product_type" in result["message"]
    assert "Terms of Use" not in result["message"]
    assert "terms_url" not in result


@pytest.mark.asyncio
async def test_forecast_schema_error_suggests_latest_cycle_query(tmp_path, monkeypatch):
    from aero.adapters.cds_adapter import CDSAdapter
    from aero.core.config import save_ads_credentials

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    (tmp_path / "aero.yaml").write_text("output:\n  data_dir: data\n")
    save_ads_credentials("https://ads.atmosphere.copernicus.eu/api", "ads-token")

    async def fake_submit(self, **kwargs):
        raise RuntimeError("400 Client Error: Bad Request\ninvalid request")

    monkeypatch.setattr(CDSAdapter, "submit", fake_submit)

    result = await download_cams(
        dataset_id=FORECAST_DATASET_ID,
        variables=["particulate_matter_2.5um"],
        start_date="2026-06-13",
        end_date="2026-06-13",
        times=["00:00"],
        leadtime_hours=[24],
    )

    assert result["status"] == "error"
    assert result["retry_same_request"] is False
    assert result["suggested_tool"] == "get_cams_latest_forecast_cycle"
