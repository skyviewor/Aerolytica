"""Tests for IFS adapter — index parsing and entry selection."""

from __future__ import annotations

import pytest

from meteora.adapters.ifs_adapter import (
    _build_grib_url,
    _build_index_url,
    _friendly_http_error,
    _normalize_cycle,
    _normalize_date,
    _normalize_step,
    _normalize_variables,
    _normalize_levtype,
    _normalize_levels,
    _normalize_levelist,
    _parse_ifs_index,
    _request_with_retry,
    _retry_message,
    _select_ifs_entries,
    is_ifs_step_available,
    ifs_forecast_steps_for_range,
)
import httpx

SAMPLE_INDEX = """\
{"domain":"g","date":"20260604","time":"0000","expver":"0001","class":"od","type":"fc","stream":"oper","levtype":"sfc","step":"3","param":"tp","_offset":0,"_length":587738}
{"domain":"g","date":"20260604","time":"0000","expver":"0001","class":"od","type":"fc","stream":"oper","levtype":"sfc","step":"3","param":"sp","_offset":587738,"_length":540441}
{"domain":"g","date":"20260604","time":"0000","expver":"0001","class":"od","type":"fc","stream":"oper","levtype":"sfc","step":"3","param":"2t","_offset":1128179,"_length":838772}
{"domain":"g","date":"20260604","time":"0000","expver":"0001","class":"od","type":"fc","stream":"oper","levtype":"pl","step":"3","param":"z","levelist":"500","_offset":1966951,"_length":876588}
{"domain":"g","date":"20260604","time":"0000","expver":"0001","class":"od","type":"fc","stream":"oper","levtype":"pl","step":"3","param":"t","levelist":"500","_offset":2843539,"_length":876588}
{"domain":"g","date":"20260604","time":"0000","expver":"0001","class":"od","type":"fc","stream":"oper","levtype":"pl","step":"3","param":"t","levelist":"850","_offset":3720127,"_length":887310}
"""


class TestParseIFSIndex:
    def test_parse_valid_index(self):
        entries = _parse_ifs_index(SAMPLE_INDEX)
        assert len(entries) == 6

    def test_parse_first_entry(self):
        entries = _parse_ifs_index(SAMPLE_INDEX)
        assert entries[0].param == "tp"
        assert entries[0].levtype == "sfc"
        assert entries[0].levelist is None
        assert entries[0].offset == 0
        assert entries[0].length == 587738
        assert entries[0].range_header == "bytes=0-587737"

    def test_parse_pl_entry(self):
        entries = _parse_ifs_index(SAMPLE_INDEX)
        pl_entry = entries[3]
        assert pl_entry.param == "z"
        assert pl_entry.levtype == "pl"
        assert pl_entry.levelist == "500"
        assert pl_entry.offset == 1966951
        assert pl_entry.range_header == "bytes=1966951-2843538"

    def test_empty_input(self):
        entries = _parse_ifs_index("")
        assert entries == []

    def test_invalid_json_lines(self):
        text = "not valid json\n{\"param\":\"2t\",\"_offset\":0,\"_length\":100}\n"
        entries = _parse_ifs_index(text)
        assert len(entries) == 1
        assert entries[0].param == "2t"
        assert entries[0].offset == 0
        assert entries[0].length == 100


class TestSelectIFSEntries:
    def _entries(self):
        return _parse_ifs_index(SAMPLE_INDEX)

    def test_select_by_param_only(self):
        entries = self._entries()
        selected, missing = _select_ifs_entries(entries, ["tp"])
        assert len(selected) == 1
        assert selected[0].param == "tp"
        assert missing == []

    def test_select_by_param_and_levtype(self):
        entries = self._entries()
        selected, missing = _select_ifs_entries(entries, ["t"], levtype="pl")
        assert len(selected) == 2
        assert all(e.param == "t" and e.levtype == "pl" for e in selected)

    def test_select_by_param_levtype_and_level(self):
        entries = self._entries()
        selected, missing = _select_ifs_entries(
            entries, ["t"], levtype="pl", levels=["500"]
        )
        assert len(selected) == 1
        assert selected[0].levelist == "500"

    def test_missing_variable(self):
        entries = self._entries()
        selected, missing = _select_ifs_entries(entries, ["nonexistent"])
        assert len(selected) == 0
        assert len(missing) == 1
        assert missing[0]["variable"] == "nonexistent"

    def test_case_insensitive_variables(self):
        entries = self._entries()
        selected, _ = _select_ifs_entries(entries, ["TP", "SP"])
        assert len(selected) == 2

    def test_missing_level(self):
        entries = self._entries()
        selected, missing = _select_ifs_entries(
            entries, ["z"], levtype="pl", levels=["1000"]
        )
        assert len(selected) == 0
        assert len(missing) == 1
        assert missing[0]["level"] == "1000"


class TestBuildURLs:
    def test_build_grib_url(self):
        url = _build_grib_url(
            "https://data.ecmwf.int/forecasts",
            "20260604",
            "00",
            24,
        )
        assert "20260604/00z/ifs/0p25/oper/" in url
        assert "20260604000000-24h-oper-fc.grib2" in url

    def test_build_index_url(self):
        grib_url = "https://data.ecmwf.int/forecasts/20260604/00z/ifs/0p25/oper/20260604000000-24h-oper-fc.grib2"
        index_url = _build_index_url(grib_url)
        assert index_url.endswith(".index")
        assert ".grib2" not in index_url


class TestRetryMessages:
    def test_429_retry_message_is_natural_language(self):
        message = _retry_message("读取 IFS 索引", 429, 1, 2)

        assert "请求过于频繁" in message
        assert "ECMWF 正在限流" in message
        assert "自动重试" in message

    def test_429_final_error_is_natural_language(self):
        response = httpx.Response(429, request=httpx.Request("GET", "https://example.com/file"))

        message = _friendly_http_error("下载 IFS GRIB 分块", "https://example.com/file", response)

        assert "429 Too Many Requests" in message
        assert "请求过于频繁" in message
        assert "减少并发" in message

    def test_request_with_retry_retries_429(monkeypatch):
        calls = []
        sleeps = []

        def fake_sleep(delay):
            sleeps.append(delay)

        def request():
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(
                    429,
                    headers={"retry-after": "1"},
                    request=httpx.Request("GET", "https://example.com/file"),
                )
            return httpx.Response(
                200,
                text="ok",
                request=httpx.Request("GET", "https://example.com/file"),
            )

        monkeypatch.setattr("meteora.adapters.ifs_adapter.time.sleep", fake_sleep)

        response = _request_with_retry(
            request,
            url="https://example.com/file",
            action="读取 IFS 索引",
        )

        assert response.text == "ok"
        assert len(calls) == 2
        assert sleeps == [1.0]


class TestNormalizers:
    def test_normalize_date(self):
        assert _normalize_date("20260604") == "20260604"
        assert _normalize_date("2026-06-04") == "20260604"
        with pytest.raises(ValueError):
            _normalize_date("abc")

    def test_normalize_cycle(self):
        assert _normalize_cycle("00") == "00"
        assert _normalize_cycle("0") == "00"
        assert _normalize_cycle("06z") == "06"
        with pytest.raises(ValueError):
            _normalize_cycle("03")

    def test_normalize_step(self):
        assert _normalize_step(0) == 0
        assert _normalize_step(240) == 240
        with pytest.raises(ValueError):
            _normalize_step(400)

    def test_normalize_variables(self):
        assert _normalize_variables(["TP", "Sp", "2T"]) == ["tp", "sp", "2t"]
        with pytest.raises(ValueError):
            _normalize_variables([])

    def test_normalize_levtype(self):
        assert _normalize_levtype("SFC") == "sfc"
        assert _normalize_levtype("pl") == "pl"
        assert _normalize_levtype("") is None

    def test_normalize_levels(self):
        assert _normalize_levels(["500", "850"]) == ["500", "850"]
        assert _normalize_levels(None) is None


class TestStepAvailability:
    def test_00z_steps_available(self):
        steps = ifs_forecast_steps_for_range(
            start_step=0,
            end_step=12,
            cycle="00",
        )
        assert steps == [0, 3, 6, 9, 12]

    def test_00z_6h_cadence(self):
        steps = ifs_forecast_steps_for_range(
            start_step=150,
            end_step=162,
            cycle="00",
        )
        assert steps == [150, 156, 162]

    def test_06z_max_90h(self):
        steps = ifs_forecast_steps_for_range(
            start_step=0,
            end_step=96,
            cycle="06",
        )
        assert steps == [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 33, 36, 39, 42, 45, 48, 51, 54, 57, 60, 63, 66, 69, 72, 75, 78, 81, 84, 87, 90]
        assert len(steps) == 31

    def test_is_step_available(self):
        assert is_ifs_step_available(0, "00") is True
        assert is_ifs_step_available(3, "00") is True
        assert is_ifs_step_available(150, "00") is True
        assert is_ifs_step_available(156, "00") is True
        assert is_ifs_step_available(153, "00") is False
        assert is_ifs_step_available(144, "00") is True
        assert is_ifs_step_available(147, "00") is False
        assert is_ifs_step_available(0, "06") is True
        assert is_ifs_step_available(93, "06") is False
        assert is_ifs_step_available(90, "06") is True
