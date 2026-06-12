"""Tests for IFS availability helpers."""

from __future__ import annotations

import re

import pytest

from aero.data.ifs_availability import (
    normalize_source,
    _parse_ifs_date_prefixes,
    _parse_ifs_cycle_prefixes,
    _parse_s3_list_bucket,
    VALID_SOURCES,
)


class TestNormalizeSource:
    def test_auto(self):
        assert normalize_source("auto") == "auto"

    def test_ecmwf(self):
        assert normalize_source("ecmwf") == "ecmwf"

    def test_aws(self):
        assert normalize_source("aws") == "aws"

    def test_default_auto(self):
        assert normalize_source(None) == "auto"

    def test_invalid_source(self):
        with pytest.raises(ValueError):
            normalize_source("invalid")

    def test_case_insensitive(self):
        assert normalize_source("AuTo") == "auto"


class TestParseDatePrefixes:
    def test_parse_ecmwf_dates(self):
        html = (
            '<html><body>'
            '<a href="/forecasts/20260604/">20260604</a>'
            '<a href="/forecasts/20260603/">20260603</a>'
            '<a href="/forecasts/20260605/">20260605</a>'
            '</body></html>'
        )
        dates = _parse_ifs_date_prefixes(html)
        assert dates == ["20260603", "20260604", "20260605"]

    def test_parse_plain_dates(self):
        text = "20260604/\n20260605/\n"
        dates = _parse_ifs_date_prefixes(text)
        assert dates == ["20260604", "20260605"]

    def test_no_dates(self):
        assert _parse_ifs_date_prefixes("no dates here") == []


class TestParseCyclePrefixes:
    def test_parse_cycles(self):
        html = (
            '<html><body>'
            '<a href="00z/">00z/</a>'
            '<a href="06z/">06z/</a>'
            '<a href="12z/">12z/</a>'
            '<a href="18z/">18z/</a>'
            '</body></html>'
        )
        cycles = _parse_ifs_cycle_prefixes(html)
        assert cycles == ["00", "06", "12", "18"]

    def test_filters_invalid_cycles(self):
        html = '<a href="03z/">03z/</a><a href="00z/">00z/</a>'
        cycles = _parse_ifs_cycle_prefixes(html)
        assert cycles == ["00"]

    def test_no_cycles(self):
        assert _parse_ifs_cycle_prefixes("no cycles here") == []


class TestParseS3ListBucket:
    def test_parse_common_prefixes(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>ecmwf-forecasts</Name>
  <Prefix></Prefix>
  <Delimiter>/</Delimiter>
  <CommonPrefixes>
    <Prefix>20260604/</Prefix>
  </CommonPrefixes>
  <CommonPrefixes>
    <Prefix>20260605/</Prefix>
  </CommonPrefixes>
  <IsTruncated>false</IsTruncated>
</ListBucketResult>"""
        result = _parse_s3_list_bucket(xml)
        assert result["common_prefixes"] == ["20260604/", "20260605/"]
        assert result["contents"] == []
        assert result["next_continuation_token"] is None

    def test_parse_contents(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>ecmwf-forecasts</Name>
  <Prefix>20260604/00z/</Prefix>
  <Contents>
    <Key>20260604/00z/ifs/0p25/oper/20260604000000-0h-oper-fc.grib2</Key>
  </Contents>
</ListBucketResult>"""
        result = _parse_s3_list_bucket(xml)
        assert "20260604/00z/ifs/0p25/oper/20260604000000-0h-oper-fc.grib2" in result["contents"]

    def test_continuation_token(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>ecmwf-forecasts</Name>
  <IsTruncated>true</IsTruncated>
  <NextContinuationToken>abc123</NextContinuationToken>
</ListBucketResult>"""
        result = _parse_s3_list_bucket(xml)
        assert result["next_continuation_token"] == "abc123"
