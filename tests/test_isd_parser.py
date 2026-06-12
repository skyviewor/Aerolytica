"""Tests for human-readable NOAA ISD parsing."""

import csv

from aero.data.isd_parser import decode_isd_row, parse_isd_csv
from aero.toolbox.tools.observations import _inspect_csv_table


def test_decode_isd_row_converts_conventional_weather_fields():
    decoded = decode_isd_row(
        {
            "STATION": "58362099999",
            "NAME": "SHANGHAI, CH",
            "DATE": "2025-07-01T00:00:00",
            "REPORT_TYPE": "FM-12",
            "SOURCE": "4",
            "QUALITY_CONTROL": "V020",
            "LATITUDE": "31.4",
            "LONGITUDE": "121.4666666",
            "ELEVATION": "4.0",
            "TMP": "+0315,1",
            "DEW": "+0238,1",
            "WND": "176,1,N,0041,1",
            "SLP": "10109,1",
            "MA1": "99999,9,10099,1",
            "VIS": "030000,1,9,9",
            "CIG": "99999,9,9,9",
            "MW1": "60,1",
            "AA1": "24,0000,9,1",
            "OC1": "0074,1",
        }
    )

    assert decoded["temperature_c"] == 31.5
    assert decoded["dew_point_c"] == 23.8
    assert decoded["relative_humidity_pct"] == 63.7
    assert decoded["wind_direction_deg"] == 176.0
    assert decoded["wind_direction_type"] == "正常风向"
    assert decoded["wind_speed_m_s"] == 4.1
    assert decoded["wind_gust_m_s"] == 7.4
    assert decoded["sea_level_pressure_hpa"] == 1010.9
    assert decoded["station_pressure_hpa"] == 1009.9
    assert decoded["visibility_m"] == 30000.0
    assert decoded["ceiling_height_m"] is None
    assert decoded["present_weather"] == "间歇轻雨"
    assert decoded["precipitation_24h_mm"] == 0.0
    assert decoded["report_type_name"] == "SYNOP 地面天气报"


def test_parse_isd_csv_writes_human_readable_table(tmp_path):
    source = tmp_path / "raw.csv"
    destination = tmp_path / "parsed.csv"
    # Build the input with csv so embedded ISD commas are quoted correctly.
    with source.open("w", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=(
                "STATION",
                "DATE",
                "NAME",
                "REPORT_TYPE",
                "SOURCE",
                "QUALITY_CONTROL",
                "TMP",
                "DEW",
                "WND",
                "SLP",
                "MA1",
                "VIS",
                "CIG",
            ),
        )
        writer.writeheader()
        writer.writerow(
            {
                "STATION": "54511099999",
                "DATE": "2025-07-01T00:00:00",
                "NAME": "BEIJING CAPITAL INTERNATIONAL AIRPORT, CH",
                "REPORT_TYPE": "FM-15",
                "SOURCE": "4",
                "QUALITY_CONTROL": "V020",
                "TMP": "+0245,1",
                "DEW": "+0230,1",
                "WND": "181,1,N,0021,1",
                "SLP": "10051,1",
                "MA1": "99999,9,10012,1",
                "VIS": "003100,1,9,9",
                "CIG": "99999,9,9,9",
            }
        )

    summary = parse_isd_csv(source, destination)

    row = next(csv.DictReader(destination.open()))
    assert summary["rows"] == 1
    assert summary["report_types"] == {"FM-15": "METAR 机场例行天气报"}
    assert row["station_name"] == "BEIJING CAPITAL INTERNATIONAL AIRPORT, CH"
    assert row["temperature_c"] == "24.5"
    assert row["relative_humidity_pct"] == "91.4"


def test_inspect_csv_table_summarizes_numeric_and_text_columns(tmp_path):
    source = tmp_path / "weather.csv"
    with source.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=("station", "temperature_c", "weather"))
        writer.writeheader()
        writer.writerows(
            (
                {"station": "A", "temperature_c": "24.5", "weather": "rain"},
                {"station": "A", "temperature_c": "37.1", "weather": "clear"},
                {"station": "B", "temperature_c": "", "weather": "rain"},
            )
        )

    result = _inspect_csv_table(source, None, 2)

    assert result["rows"] == 3
    assert result["summary"]["temperature_c"] == {
        "non_null": 2,
        "missing": 1,
        "type": "numeric",
        "min": 24.5,
        "max": 37.1,
        "mean": 30.8,
    }
    assert result["summary"]["weather"]["top_values"][0] == {"value": "rain", "count": 2}


def test_inspect_csv_table_rejects_unknown_column(tmp_path):
    source = tmp_path / "weather.csv"
    source.write_text("temperature_c\n24.5\n")

    try:
        _inspect_csv_table(source, ["humidity_pct"], 5)
    except ValueError as exc:
        assert str(exc) == "字段不存在：humidity_pct"
    else:
        raise AssertionError("unknown columns must fail")
