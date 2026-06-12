import pytest

from aero.data.gfs_availability import (
    AWS_GFS_BASE,
    NOMADS_GFS_BASE,
    GFSObjectAvailability,
    gfs_forecast_hours_for_range,
    parse_gfs_cycle_prefixes,
    parse_gfs_date_prefixes,
    parse_s3_list_bucket,
    resolve_gfs_source,
)


def test_parse_nomads_dates_and_cycles():
    html = """
    <a href="gfs.20260601/">gfs.20260601/</a>
    <a href="gfs.20260604/">gfs.20260604/</a>
    <a href="00/">00/</a><a href="06/">06/</a><a href="junk/">junk/</a>
    """

    assert parse_gfs_date_prefixes(html) == ["20260601", "20260604"]
    assert parse_gfs_cycle_prefixes(html) == ["00", "06"]


def test_parse_s3_list_bucket_common_prefixes_and_pagination():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <NextContinuationToken>abc/123==</NextContinuationToken>
      <CommonPrefixes><Prefix>gfs.20210101/</Prefix></CommonPrefixes>
      <Contents><Key>gfs.20210101/00/example.idx</Key></Contents>
    </ListBucketResult>
    """

    parsed = parse_s3_list_bucket(xml)
    assert parsed["common_prefixes"] == ["gfs.20210101/"]
    assert parsed["contents"] == ["gfs.20210101/00/example.idx"]
    assert parsed["next_continuation_token"] == "abc/123=="


def test_gfs_forecast_schedule_is_hourly_through_120():
    schedule = gfs_forecast_hours_for_range(duration_hours=12, product="pgrb2.0p25")

    assert schedule["forecast_hours"] == list(range(13))
    assert schedule["intervals"] == [1]
    assert schedule["unavailable_hours"] == []


def test_gfs_forecast_schedule_uses_three_hourly_for_0p50_product():
    schedule = gfs_forecast_hours_for_range(duration_hours=12, product="pgrb2.0p50")

    assert schedule["forecast_hours"] == [0, 3, 6, 9, 12]
    assert schedule["intervals"] == [3]
    assert schedule["unavailable_hours"] == [1, 2, 4, 5, 7, 8, 10, 11]


def test_gfs_forecast_schedule_skips_unavailable_hours_after_120():
    schedule = gfs_forecast_hours_for_range(
        start_hour=118,
        end_hour=126,
        product="pgrb2.0p25",
    )

    assert schedule["forecast_hours"] == [118, 119, 120, 123, 126]
    assert schedule["unavailable_hours"] == [121, 122, 124, 125]


def test_gfs_forecast_schedule_uses_legacy_0p25_history_after_240_hours():
    schedule = gfs_forecast_hours_for_range(
        start_hour=238,
        end_hour=264,
        product="pgrb2.0p25",
        date="20200101",
    )

    assert schedule["forecast_hours"] == [240, 252, 264]
    assert 243 in schedule["unavailable_hours"]
    assert 246 in schedule["unavailable_hours"]
    assert 249 in schedule["unavailable_hours"]
    assert schedule["intervals"] == [12]
    assert "每 12 小时" in schedule["note"]


@pytest.mark.asyncio
async def test_resolve_gfs_source_prefers_nomads(monkeypatch):
    async def fake_check(**kwargs):
        return GFSObjectAvailability(
            source=kwargs["source"],
            available=kwargs["source"] == "nomads",
            base_url=kwargs["base_url"],
            grib_url=f"{kwargs['base_url']}/file",
            idx_url=f"{kwargs['base_url']}/file.idx",
            reason="ok" if kwargs["source"] == "nomads" else "missing",
        )

    monkeypatch.setattr("aero.data.gfs_availability.check_gfs_object", fake_check)

    decision = await resolve_gfs_source(
        date="2026-06-04",
        cycle="00",
        product="pgrb2.0p25",
        forecast_hour=0,
    )

    assert decision.available is True
    assert decision.selected_source == "nomads"
    assert decision.selected.base_url == NOMADS_GFS_BASE


@pytest.mark.asyncio
async def test_resolve_gfs_source_falls_back_to_aws(monkeypatch):
    async def fake_check(**kwargs):
        return GFSObjectAvailability(
            source=kwargs["source"],
            available=kwargs["source"] == "aws",
            base_url=kwargs["base_url"],
            grib_url=f"{kwargs['base_url']}/file",
            idx_url=f"{kwargs['base_url']}/file.idx",
            reason="ok" if kwargs["source"] == "aws" else "missing",
            status_code=200 if kwargs["source"] == "aws" else 404,
        )

    monkeypatch.setattr("aero.data.gfs_availability.check_gfs_object", fake_check)

    decision = await resolve_gfs_source(
        date="2021-01-02",
        cycle="00",
        product="pgrb2.0p25",
        forecast_hour=0,
    )

    assert decision.available is True
    assert decision.selected_source == "aws"
    assert decision.selected.base_url == AWS_GFS_BASE
    assert "AWS" in decision.reason


@pytest.mark.asyncio
async def test_resolve_gfs_source_honors_explicit_nomads(monkeypatch):
    calls = []

    async def fake_check(**kwargs):
        calls.append(kwargs["source"])
        return GFSObjectAvailability(
            source=kwargs["source"],
            available=False,
            base_url=kwargs["base_url"],
            grib_url=f"{kwargs['base_url']}/file",
            idx_url=f"{kwargs['base_url']}/file.idx",
            reason="missing",
            status_code=404,
        )

    monkeypatch.setattr("aero.data.gfs_availability.check_gfs_object", fake_check)

    decision = await resolve_gfs_source(
        date="2021-01-02",
        cycle="00",
        product="pgrb2.0p25",
        forecast_hour=0,
        source="nomads",
    )

    assert decision.available is False
    assert calls == ["nomads"]
