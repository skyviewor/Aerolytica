import pytest

from meteora.adapters.gfs_adapter import GFSDownloadFile, GFSIndexEntry
from meteora.data.gfs_availability import GFSAvailabilityDecision, GFSObjectAvailability


def _decision(source="nomads"):
    selected = GFSObjectAvailability(
        source=source,
        available=True,
        base_url=f"https://example.com/{source}",
        grib_url=f"https://example.com/{source}/gfs",
        idx_url=f"https://example.com/{source}/gfs.idx",
        reason="ok",
        status_code=200,
    )
    missing = GFSObjectAvailability(
        source="aws" if source == "nomads" else "nomads",
        available=False,
        base_url="https://example.com/missing",
        grib_url="https://example.com/missing/gfs",
        idx_url="https://example.com/missing/gfs.idx",
        reason="missing",
        status_code=404,
    )
    return GFSAvailabilityDecision(
        requested_source="auto",
        selected_source=source,
        available=True,
        date="20260604",
        cycle="00",
        product="pgrb2.0p25",
        forecast_hour=0,
        nomads=selected if source == "nomads" else missing,
        aws=selected if source == "aws" else missing,
        reason="ok",
    )


@pytest.mark.asyncio
async def test_download_gfs_records_can_be_listed_and_queried(tmp_path, monkeypatch):
    from meteora.toolbox import builtin_tools

    entry = GFSIndexEntry(
        index=1,
        start_byte=0,
        end_byte=99,
        run="d=2026060400",
        variable="TMP",
        level="500 mb",
        forecast="anl",
        raw="1:0:d=2026060400:TMP:500 mb:anl:",
    )
    output = tmp_path / "gfs_20260604_00z_f000_TMP.grib2"
    output.write_bytes(b"GRIB")

    async def fake_resolve(**kwargs):
        return _decision()

    async def fake_download_one(self, **kwargs):
        return GFSDownloadFile(
            forecast_hour=0,
            idx_url="https://example.com/gfs.idx",
            grib_url="https://example.com/gfs",
            file_path=output,
            selected_entries=[entry],
            missing=[],
            downloaded_bytes=4,
        )

    monkeypatch.setattr("meteora.data.gfs_availability.resolve_gfs_source", fake_resolve)
    monkeypatch.setattr("meteora.adapters.gfs_adapter.GFSAdapter.download_one", fake_download_one)
    monkeypatch.setattr("meteora.toolbox.tools.gfs.find_project_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "meteora.toolbox.tools.download_records.find_project_dir",
        lambda: tmp_path,
    )

    result = await builtin_tools.download_gfs(
        date="20260604",
        cycle="00",
        forecast_hours=[0],
        variables=["TMP"],
        levels=["500 mb"],
    )

    assert result["status"] == "success"
    assert result["files"][0]["selected_messages"] == 1
    assert result["files"][0]["source_used"] == "nomads"

    listed = await builtin_tools.list_downloads(limit=10)
    assert listed["downloads"][0]["source"] == "gfs"
    assert listed["downloads"][0]["dataset_id"] == "gfs-pgrb2-0p25"

    queried = await builtin_tools.query_download(download_id=result["files"][0]["download_id"])
    assert queried["source"] == "gfs"
    assert queried["file_path"] == str(output)


@pytest.mark.asyncio
async def test_download_gfs_reports_validation_errors(monkeypatch):
    from meteora.toolbox import builtin_tools

    async def fake_resolve(**kwargs):
        raise ValueError("cycle 只支持 00、06、12、18")

    monkeypatch.setattr("meteora.data.gfs_availability.resolve_gfs_source", fake_resolve)

    result = await builtin_tools.download_gfs(
        date="20260604",
        cycle="03",
        forecast_hours=[0],
        variables=["TMP"],
    )

    assert result["status"] == "error"
    assert "cycle" in result["message"]


@pytest.mark.asyncio
async def test_download_gfs_records_aws_fallback_source(tmp_path, monkeypatch):
    from meteora.toolbox import builtin_tools

    entry = GFSIndexEntry(
        index=1,
        start_byte=0,
        end_byte=99,
        run="d=2021010200",
        variable="TMP",
        level="500 mb",
        forecast="anl",
        raw="1:0:d=2021010200:TMP:500 mb:anl:",
    )
    output = tmp_path / "gfs_aws.grib2"
    output.write_bytes(b"GRIB")

    async def fake_resolve(**kwargs):
        return _decision("aws")

    async def fake_download_one(self, **kwargs):
        assert self._base_url == "https://example.com/aws"
        return GFSDownloadFile(
            forecast_hour=0,
            idx_url="https://example.com/aws/gfs.idx",
            grib_url="https://example.com/aws/gfs",
            file_path=output,
            selected_entries=[entry],
            missing=[],
            downloaded_bytes=4,
        )

    monkeypatch.setattr("meteora.data.gfs_availability.resolve_gfs_source", fake_resolve)
    monkeypatch.setattr("meteora.adapters.gfs_adapter.GFSAdapter.download_one", fake_download_one)
    monkeypatch.setattr("meteora.toolbox.tools.gfs.find_project_dir", lambda: tmp_path)

    result = await builtin_tools.download_gfs(
        date="20210102",
        cycle="00",
        forecast_hours=[0],
        variables=["TMP"],
    )

    assert result["status"] == "success"
    assert result["sources_used"] == ["aws"]
    assert result["files"][0]["source_used"] == "aws"


@pytest.mark.asyncio
async def test_download_gfs_skips_forecast_hour_without_matching_field(tmp_path, monkeypatch):
    from meteora.toolbox import builtin_tools

    entry = GFSIndexEntry(
        index=1,
        start_byte=0,
        end_byte=99,
        run="d=2026061000",
        variable="APCP",
        level="surface",
        forecast="0-1 hour acc fcst",
        raw="1:0:d=2026061000:APCP:surface:0-1 hour acc fcst:",
    )
    output = tmp_path / "gfs_20260610_00z_f001_APCP.grib2"
    output.write_bytes(b"GRIB")

    async def fake_resolve(**kwargs):
        decision = _decision()
        return GFSAvailabilityDecision(
            **{
                **decision.__dict__,
                "forecast_hour": kwargs["forecast_hour"],
            }
        )

    async def fake_download_one(self, **kwargs):
        if kwargs["forecast_hour"] == 0:
            raise RuntimeError("GFS .idx 中没有找到匹配字段")
        return GFSDownloadFile(
            forecast_hour=kwargs["forecast_hour"],
            idx_url="https://example.com/gfs.idx",
            grib_url="https://example.com/gfs",
            file_path=output,
            selected_entries=[entry],
            missing=[],
            downloaded_bytes=4,
        )

    monkeypatch.setattr("meteora.data.gfs_availability.resolve_gfs_source", fake_resolve)
    monkeypatch.setattr("meteora.adapters.gfs_adapter.GFSAdapter.download_one", fake_download_one)
    monkeypatch.setattr("meteora.toolbox.tools.gfs.find_project_dir", lambda: tmp_path)

    result = await builtin_tools.download_gfs(
        date="20260610",
        cycle="00",
        forecast_hours=[0, 1],
        variables=["APCP"],
    )

    assert result["status"] == "success"
    assert result["total_files"] == 1
    assert result["files"][0]["forecast_hour"] == 1
    assert result["skipped_forecast_hours"][0]["forecast_hour"] == 0


@pytest.mark.asyncio
async def test_inspect_gfs_inventory_unavailable_is_not_tool_error(monkeypatch):
    from meteora.toolbox import builtin_tools

    async def fake_resolve(**kwargs):
        selected = GFSObjectAvailability(
            source="nomads",
            available=False,
            base_url="https://example.com/nomads",
            grib_url="https://example.com/nomads/gfs",
            idx_url="https://example.com/nomads/gfs.idx",
            reason="not published yet",
            status_code=404,
        )
        return GFSAvailabilityDecision(
            requested_source="auto",
            selected_source=None,
            available=False,
            date="20260610",
            cycle="06",
            product="pgrb2.0p25",
            forecast_hour=12,
            nomads=selected,
            aws=selected,
            reason="官网和 AWS 历史归档都没有找到这个目标文件。",
        )

    monkeypatch.setattr("meteora.data.gfs_availability.resolve_gfs_source", fake_resolve)

    result = await builtin_tools.inspect_gfs_inventory(
        date="20260610",
        cycle="06",
        forecast_hour=12,
        variables=["APCP"],
    )

    assert result["status"] == "unavailable"
    assert "暂不可用" in result["message"]


@pytest.mark.asyncio
async def test_search_gfs_variables_warns_when_only_grib_definition_exists(monkeypatch):
    from meteora.toolbox import builtin_tools

    async def fake_inventory():
        return {"records": []}

    async def fake_parameters():
        return [
            {
                "discipline": 3,
                "category": 5,
                "number": 0,
                "abbrev": "ISSTMP",
                "parameter": "Interface Sea Surface Temperature",
                "units": "K",
                "source_url": "https://example.com/grib2_table4-2-3-5.shtml",
            }
        ]

    monkeypatch.setattr("meteora.data.gfs_products.get_gfs_product_inventory", fake_inventory)
    monkeypatch.setattr("meteora.data.gfs_params.get_gfs_parameters", fake_parameters)

    result = await builtin_tools.search_gfs_variables(keyword="SST")

    assert result["found"] is True
    assert result["downloadable_in_gfs_inventory"] is False
    assert "不要自动改用近似变量" in result["warning"]


@pytest.mark.asyncio
async def test_check_gfs_availability_returns_cached_ranges(monkeypatch):
    from meteora.toolbox import builtin_tools

    async def fake_summary(refresh=False):
        assert refresh is True
        return {
            "nomads": {
                "source": "nomads",
                "earliest_date": "20260601",
                "latest_date": "20260604",
                "cycles": ["00", "06"],
                "checked_at": "2026-06-04T00:00:00+00:00",
                "source_url": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/",
                "notes": None,
            },
            "aws": {
                "source": "aws",
                "earliest_date": "20210101",
                "latest_date": "20260604",
                "cycles": ["00", "06", "12", "18"],
                "checked_at": "2026-06-04T00:00:00+00:00",
                "source_url": "https://noaa-gfs-bdp-pds.s3.amazonaws.com",
                "notes": None,
            },
        }

    monkeypatch.setattr("meteora.data.gfs_availability.get_gfs_availability", fake_summary)

    result = await builtin_tools.check_gfs_availability(refresh=True)

    assert result["status"] == "success"
    assert result["mode"] == "range"
    assert result["aws"]["earliest_date"] == "20210101"


@pytest.mark.asyncio
async def test_inspect_gfs_inventory_reports_variable_levels(monkeypatch):
    from meteora.toolbox import builtin_tools

    idx_text = "\n".join(
        [
            "1:0:d=2026060412:APCP:surface:0-1 hour acc fcst:",
            "2:100:d=2026060412:APCP:surface:0-3 hour acc fcst:",
            "3:250:d=2026060412:TMP:2 m above ground:3 hour fcst:",
        ]
    )

    async def fake_resolve(**kwargs):
        return _decision()

    def fake_fetch_text(url):
        assert url.endswith(".idx")
        return idx_text

    monkeypatch.setattr("meteora.data.gfs_availability.resolve_gfs_source", fake_resolve)
    monkeypatch.setattr(
        "meteora.adapters.gfs_adapter.GFSAdapter._fetch_text", staticmethod(fake_fetch_text)
    )

    result = await builtin_tools.inspect_gfs_inventory(
        date="20260604",
        cycle="12",
        forecast_hour=3,
        variables=["APCP"],
    )

    assert result["status"] == "success"
    assert result["variables"] == ["APCP"]
    assert result["matched"] == 2
    assert {item["forecast"] for item in result["inventory"]} == {
        "0-1 hour acc fcst",
        "0-3 hour acc fcst",
    }
    assert {item["level"] for item in result["inventory"]} == {"surface"}


@pytest.mark.asyncio
async def test_get_gfs_forecast_schedule_uses_hourly_first_120_hours():
    from meteora.toolbox import builtin_tools

    result = await builtin_tools.get_gfs_forecast_schedule(duration_hours=12)

    assert result["status"] == "success"
    assert result["forecast_hours"] == list(range(13))
    assert result["intervals"] == [1]


@pytest.mark.asyncio
async def test_get_gfs_forecast_schedule_uses_three_hourly_for_0p50_product():
    from meteora.toolbox import builtin_tools

    result = await builtin_tools.get_gfs_forecast_schedule(
        duration_hours=12,
        product="pgrb2.0p50",
    )

    assert result["status"] == "success"
    assert result["forecast_hours"] == [0, 3, 6, 9, 12]
    assert result["unavailable_hours"] == [1, 2, 4, 5, 7, 8, 10, 11]


@pytest.mark.asyncio
async def test_get_gfs_forecast_schedule_uses_historical_0p25_date_rule():
    from meteora.toolbox import builtin_tools

    result = await builtin_tools.get_gfs_forecast_schedule(
        start_hour=238,
        end_hour=264,
        product="pgrb2.0p25",
        date="2020-01-01",
    )

    assert result["status"] == "success"
    assert result["date"] == "20200101"
    assert result["forecast_hours"] == [240, 252, 264]
    assert result["intervals"] == [12]


@pytest.mark.asyncio
async def test_get_gfs_forecast_schedule_accepts_cycle_context():
    from meteora.toolbox import builtin_tools

    result = await builtin_tools.get_gfs_forecast_schedule(
        duration_hours=12,
        date="20260610",
        cycle="06z",
    )

    assert result["status"] == "success"
    assert result["cycle"] == "06"
    assert result["forecast_hours"] == list(range(13))


@pytest.mark.asyncio
async def test_get_gfs_forecast_schedule_rejects_invalid_cycle():
    from meteora.toolbox import builtin_tools

    result = await builtin_tools.get_gfs_forecast_schedule(
        duration_hours=12,
        date="20260610",
        cycle="03",
    )

    assert result["status"] == "error"
    assert "cycle 只支持" in result["message"]
