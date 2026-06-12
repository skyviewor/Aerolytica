"""Decode NOAA ISD CSV observations into conventional meteorological fields."""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

REPORT_TYPES = {
    "FM-12": "SYNOP 地面天气报",
    "FM-13": "SHIP 船舶天气报",
    "FM-14": "SYNOP MOBIL 移动站天气报",
    "FM-15": "METAR 机场例行天气报",
    "FM-16": "SPECI 机场特殊天气报",
}

WIND_DIRECTION_TYPES = {
    "N": "正常风向",
    "V": "风向不定",
    "C": "静风",
    "9": "缺测",
}

WEATHER_CODES = {
    "00": "无显著天气",
    "01": "云量减少",
    "02": "天空状况无明显变化",
    "03": "云量增加",
    "04": "烟",
    "05": "霾",
    "06": "浮尘",
    "07": "扬尘或扬沙",
    "08": "尘卷风",
    "09": "沙尘暴",
    "10": "轻雾",
    "11": "浅雾",
    "12": "连续浅雾",
    "13": "闪电但无雷声",
    "17": "雷暴但无降水",
    "18": "飑",
    "19": "漏斗云",
    "20": "过去一小时有毛毛雨",
    "21": "过去一小时有雨",
    "22": "过去一小时有雪",
    "23": "过去一小时有雨夹雪",
    "24": "过去一小时有冻雨",
    "25": "过去一小时有阵雨",
    "26": "过去一小时有阵雪",
    "27": "过去一小时有冰雹",
    "28": "过去一小时有雾",
    "29": "过去一小时有雷暴",
    "30": "轻度或中度沙尘暴减弱",
    "31": "轻度或中度沙尘暴无变化",
    "32": "轻度或中度沙尘暴增强",
    "33": "强沙尘暴减弱",
    "34": "强沙尘暴无变化",
    "35": "强沙尘暴增强",
    "40": "附近有雾",
    "41": "局地雾",
    "42": "雾，天空可辨，减弱",
    "43": "雾，天空不可辨，减弱",
    "44": "雾，天空可辨，无变化",
    "45": "雾，天空不可辨，无变化",
    "46": "雾，天空可辨，增强",
    "47": "雾，天空不可辨，增强",
    "48": "雾淞雾，天空可辨",
    "49": "雾淞雾，天空不可辨",
    "50": "间歇轻毛毛雨",
    "51": "连续轻毛毛雨",
    "52": "间歇中等毛毛雨",
    "53": "连续中等毛毛雨",
    "54": "间歇强毛毛雨",
    "55": "连续强毛毛雨",
    "56": "轻冻毛毛雨",
    "57": "中等或强冻毛毛雨",
    "58": "轻毛毛雨夹雨",
    "59": "中等或强毛毛雨夹雨",
    "60": "间歇轻雨",
    "61": "连续轻雨",
    "62": "间歇中雨",
    "63": "连续中雨",
    "64": "间歇大雨",
    "65": "连续大雨",
    "66": "轻冻雨",
    "67": "中等或强冻雨",
    "68": "轻雨夹雪",
    "69": "中等或强雨夹雪",
    "70": "间歇轻雪",
    "71": "连续轻雪",
    "72": "间歇中雪",
    "73": "连续中雪",
    "74": "间歇大雪",
    "75": "连续大雪",
    "76": "冰针",
    "77": "米雪",
    "78": "孤立雪晶",
    "79": "冰粒",
    "80": "轻阵雨",
    "81": "中等或强阵雨",
    "82": "猛烈阵雨",
    "83": "轻雨夹雪阵性降水",
    "84": "中等或强雨夹雪阵性降水",
    "85": "轻阵雪",
    "86": "中等或强阵雪",
    "87": "轻冰雹或霰",
    "88": "中等或强冰雹或霰",
    "89": "轻冰雹",
    "90": "中等或强冰雹",
    "91": "过去有雷暴，当前轻雨",
    "92": "过去有雷暴，当前中等或强雨",
    "93": "过去有雷暴，当前轻雪或雨夹雪",
    "94": "过去有雷暴，当前中等或强雪或雨夹雪",
    "95": "轻或中等雷暴，无冰雹",
    "96": "轻或中等雷暴，有冰雹",
    "97": "强雷暴，无冰雹",
    "98": "雷暴伴沙尘暴",
    "99": "强雷暴，有冰雹",
}

OUTPUT_FIELDS = (
    "station_id",
    "station_name",
    "datetime_utc",
    "latitude",
    "longitude",
    "elevation_m",
    "report_type",
    "report_type_name",
    "temperature_c",
    "temperature_quality",
    "dew_point_c",
    "dew_point_quality",
    "relative_humidity_pct",
    "wind_direction_deg",
    "wind_direction_type",
    "wind_direction_quality",
    "wind_speed_m_s",
    "wind_speed_quality",
    "wind_gust_m_s",
    "wind_gust_quality",
    "sea_level_pressure_hpa",
    "sea_level_pressure_quality",
    "station_pressure_hpa",
    "station_pressure_quality",
    "visibility_m",
    "visibility_quality",
    "ceiling_height_m",
    "ceiling_quality",
    "present_weather_code",
    "present_weather",
    "present_weather_quality",
    "precipitation_1h_mm",
    "precipitation_3h_mm",
    "precipitation_6h_mm",
    "precipitation_12h_mm",
    "precipitation_24h_mm",
    "precipitation_other_mm",
    "precipitation_quality",
    "source_code",
    "quality_control_process",
)


def _parts(value: str, count: int) -> list[str]:
    values = value.split(",") if value else []
    return values + [""] * (count - len(values))


def _scaled(value: str, missing: set[str], scale: float = 10.0) -> float | None:
    if not value or value.lstrip("+-") in missing:
        return None
    try:
        return round(int(value) / scale, 3)
    except ValueError:
        return None


def _number(value: str, missing: set[str]) -> float | None:
    if not value or value.lstrip("+-") in missing:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _temperature(value: str) -> tuple[float | None, str]:
    number, quality = _parts(value, 2)[:2]
    return _scaled(number, {"9999"}), quality


def _wind(value: str) -> dict[str, Any]:
    direction, direction_quality, direction_type, speed, speed_quality = _parts(value, 5)[:5]
    return {
        "wind_direction_deg": _number(direction, {"999"}),
        "wind_direction_quality": direction_quality,
        "wind_direction_type": WIND_DIRECTION_TYPES.get(direction_type, direction_type),
        "wind_speed_m_s": _scaled(speed, {"9999"}),
        "wind_speed_quality": speed_quality,
    }


def _pressure(value: str) -> tuple[float | None, str]:
    number, quality = _parts(value, 2)[:2]
    return _scaled(number, {"99999"}), quality


def _station_pressure(value: str) -> tuple[float | None, str]:
    _, _, pressure, quality = _parts(value, 4)[:4]
    return _scaled(pressure, {"99999"}), quality


def _visibility(value: str) -> tuple[float | None, str]:
    distance, quality, _, _ = _parts(value, 4)[:4]
    return _number(distance, {"999999"}), quality


def _ceiling(value: str) -> tuple[float | None, str]:
    height, quality, _, _ = _parts(value, 4)[:4]
    return _number(height, {"99999"}), quality


def _gust(value: str) -> tuple[float | None, str]:
    speed, quality = _parts(value, 2)[:2]
    return _scaled(speed, {"9999"}), quality


def _weather(row: dict[str, str]) -> tuple[str, str, str]:
    for prefix in ("MW", "AW"):
        for index in range(1, 8):
            code, quality = _parts(row.get(f"{prefix}{index}", ""), 2)[:2]
            if code and code not in {"99", "9999"}:
                normalized = code.zfill(2)
                return normalized, WEATHER_CODES.get(normalized, f"天气代码 {normalized}"), quality
    return "", "", ""


def _precipitation(row: dict[str, str]) -> dict[str, Any]:
    values: dict[str, float] = {}
    qualities: list[str] = []
    for index in range(1, 5):
        period, depth, _, quality = _parts(row.get(f"AA{index}", ""), 4)[:4]
        amount = _scaled(depth, {"9999"})
        if amount is None:
            continue
        key = period if period in {"01", "03", "06", "12", "24"} else "other"
        values.setdefault(key, amount)
        if quality:
            qualities.append(quality)
    return {
        "precipitation_1h_mm": values.get("01"),
        "precipitation_3h_mm": values.get("03"),
        "precipitation_6h_mm": values.get("06"),
        "precipitation_12h_mm": values.get("12"),
        "precipitation_24h_mm": values.get("24"),
        "precipitation_other_mm": values.get("other"),
        "precipitation_quality": ",".join(dict.fromkeys(qualities)),
    }


def _relative_humidity(temperature: float | None, dew_point: float | None) -> float | None:
    if temperature is None or dew_point is None:
        return None
    exponent = (17.625 * dew_point) / (243.04 + dew_point) - (
        17.625 * temperature
    ) / (243.04 + temperature)
    return round(max(0.0, min(100.0, 100.0 * math.exp(exponent))), 1)


def decode_isd_row(row: dict[str, str]) -> dict[str, Any]:
    temperature, temperature_quality = _temperature(row.get("TMP", ""))
    dew_point, dew_point_quality = _temperature(row.get("DEW", ""))
    sea_level_pressure, sea_level_pressure_quality = _pressure(row.get("SLP", ""))
    station_pressure, station_pressure_quality = _station_pressure(row.get("MA1", ""))
    visibility, visibility_quality = _visibility(row.get("VIS", ""))
    ceiling, ceiling_quality = _ceiling(row.get("CIG", ""))
    gust, gust_quality = _gust(row.get("OC1", ""))
    weather_code, weather, weather_quality = _weather(row)
    report_type = row.get("REPORT_TYPE", "")
    decoded = {
        "station_id": row.get("STATION", ""),
        "station_name": row.get("NAME", ""),
        "datetime_utc": row.get("DATE", ""),
        "latitude": _number(row.get("LATITUDE", ""), {"99999"}),
        "longitude": _number(row.get("LONGITUDE", ""), {"999999"}),
        "elevation_m": _number(row.get("ELEVATION", ""), {"9999"}),
        "report_type": report_type,
        "report_type_name": REPORT_TYPES.get(report_type, report_type),
        "temperature_c": temperature,
        "temperature_quality": temperature_quality,
        "dew_point_c": dew_point,
        "dew_point_quality": dew_point_quality,
        "relative_humidity_pct": _relative_humidity(temperature, dew_point),
        **_wind(row.get("WND", "")),
        "wind_gust_m_s": gust,
        "wind_gust_quality": gust_quality,
        "sea_level_pressure_hpa": sea_level_pressure,
        "sea_level_pressure_quality": sea_level_pressure_quality,
        "station_pressure_hpa": station_pressure,
        "station_pressure_quality": station_pressure_quality,
        "visibility_m": visibility,
        "visibility_quality": visibility_quality,
        "ceiling_height_m": ceiling,
        "ceiling_quality": ceiling_quality,
        "present_weather_code": weather_code,
        "present_weather": weather,
        "present_weather_quality": weather_quality,
        **_precipitation(row),
        "source_code": row.get("SOURCE", ""),
        "quality_control_process": row.get("QUALITY_CONTROL", ""),
    }
    return {field: decoded.get(field) for field in OUTPUT_FIELDS}


def parse_isd_csv(input_path: Path, output_path: Path) -> dict[str, Any]:
    """Decode an NOAA ISD CSV into a human-readable conventional weather table."""
    report_types: set[str] = set()
    station_ids: set[str] = set()
    station_names: set[str] = set()
    preview: list[dict[str, Any]] = []
    row_count = 0
    with input_path.open(newline="") as source, output_path.open("w", newline="") as target:
        reader = csv.DictReader(source)
        if not reader.fieldnames or not {"STATION", "DATE"} <= set(reader.fieldnames):
            raise ValueError("输入文件不是有效的 NOAA ISD Global Hourly CSV")
        writer = csv.DictWriter(target, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for row in reader:
            decoded = decode_isd_row(row)
            writer.writerow(decoded)
            row_count += 1
            station_ids.add(row.get("STATION", ""))
            station_names.add(row.get("NAME", ""))
            report_types.add(row.get("REPORT_TYPE", ""))
            if len(preview) < 3:
                preview.append(decoded)
    return {
        "rows": row_count,
        "stations": sorted(station_ids - {""}),
        "station_names": sorted(station_names - {""}),
        "report_types": {
            code: REPORT_TYPES.get(code, code) for code in sorted(report_types - {""})
        },
        "columns": list(OUTPUT_FIELDS),
        "preview": preview,
    }
