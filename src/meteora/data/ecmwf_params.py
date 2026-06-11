"""ECMWF parameter database lookup helpers."""

from __future__ import annotations

import html
import re
from typing import Any

import httpx

PARAM_DB_API = "https://codes.ecmwf.int/parameter-database/api/v1"
PARAM_DB_WEB = "https://codes.ecmwf.int/grib/param-db"

CDS_TO_ECMWF_SHORTNAME = {
    "2m_temperature": "2t",
    "2m_dewpoint_temperature": "2d",
    "10m_u_component_of_wind": "10u",
    "10m_v_component_of_wind": "10v",
    "mean_sea_level_pressure": "msl",
    "surface_pressure": "sp",
    "total_precipitation": "tp",
    "convective_precipitation": "cp",
    "large_scale_precipitation": "lsp",
    "snowfall": "sf",
    "total_cloud_cover": "tcc",
    "low_cloud_cover": "lcc",
    "medium_cloud_cover": "mcc",
    "high_cloud_cover": "hcc",
    "temperature": "t",
    "u_component_of_wind": "u",
    "v_component_of_wind": "v",
    "geopotential": "z",
    "specific_humidity": "q",
    "relative_humidity": "r",
    "vertical_velocity": "w",
}

CHINESE_TO_CDS_NAME = {
    "2米气温": "2m_temperature",
    "两米气温": "2m_temperature",
    "2米温度": "2m_temperature",
    "两米温度": "2m_temperature",
    "2米露点温度": "2m_dewpoint_temperature",
    "10米纬向风": "10m_u_component_of_wind",
    "10米经向风": "10m_v_component_of_wind",
    "海平面气压": "mean_sea_level_pressure",
    "地表气压": "surface_pressure",
    "地面气压": "surface_pressure",
    "总降水": "total_precipitation",
    "对流降水": "convective_precipitation",
    "大尺度降水": "large_scale_precipitation",
    "层状降水": "large_scale_precipitation",
    "降雪": "snowfall",
    "总云量": "total_cloud_cover",
    "低云量": "low_cloud_cover",
    "中云量": "medium_cloud_cover",
    "高云量": "high_cloud_cover",
    "气温": "temperature",
    "温度": "temperature",
    "纬向风": "u_component_of_wind",
    "经向风": "v_component_of_wind",
    "位势": "geopotential",
    "比湿": "specific_humidity",
    "相对湿度": "relative_humidity",
    "垂直速度": "vertical_velocity",
    # IFS-specific parameter Chinese aliases
    "地表温度": "skin_temperature",
    "降雪量": "snowfall",
    "雪深": "snow_depth",
    "降水率": "total_precipitation",
    "蒸发": "evaporation",
    "径流": "runoff",
    "土壤温度": "soil_temperature_level_1",
    "阵风": "10m_wind_gust_since_previous_post_processing",
    "涡度": "vorticity",
    "散度": "divergence",
    "海冰": "sea_ice_thickness",
    "整层水汽": "total_column_water_vapour",
    "整层液态水": "total_column_cloud_liquid_water",
}


async def lookup_ecmwf_parameters(
    query: str | None = None,
    *,
    short_name: str | None = None,
    param_id: int | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Lookup parameter definitions from ECMWF Parameter Database."""
    if param_id is None and not (query or short_name):
        return {
            "found": False,
            "message": "query、short_name、param_id 至少提供一个。",
            "source": PARAM_DB_WEB,
        }

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        if param_id is not None:
            item = await _fetch_param_by_id(client, param_id)
            if item is None:
                return {
                    "found": False,
                    "param_id": param_id,
                    "message": f"ECMWF 参数库中未找到 paramId={param_id}。",
                    "source": f"{PARAM_DB_WEB}/?id={param_id}",
                }
            return {
                "found": True,
                "count": 1,
                "parameters": [await _format_param(client, item)],
                "source": f"{PARAM_DB_WEB}/?id={param_id}",
            }

        search_text = _normalize_search_text(query, short_name)
        try:
            results = await _search_params(client, search_text)
        except httpx.HTTPStatusError as exc:
            return {
                "found": False,
                "query": query or "",
                "short_name": short_name or CDS_TO_ECMWF_SHORTNAME.get((query or "").strip(), ""),
                "count": 0,
                "parameters": [],
                "source": f"{PARAM_DB_WEB}/?filter={search_text}",
                "message": f"ECMWF 参数库查询暂时失败：HTTP {exc.response.status_code}。",
            }
        results = _rank_results(results, query=query, short_name=short_name)[:max(1, limit)]

        return {
            "found": bool(results),
            "query": query or "",
            "short_name": short_name or CDS_TO_ECMWF_SHORTNAME.get((query or "").strip(), ""),
            "count": len(results),
            "parameters": [await _format_param(client, item) for item in results],
            "source": f"{PARAM_DB_WEB}/?filter={search_text}",
            "note": "来源为 ECMWF Parameter Database，适合核对 paramId、shortName、单位和官方定义。",
        }


async def _fetch_param_by_id(client: httpx.AsyncClient, param_id: int) -> dict[str, Any] | None:
    resp = await client.get(f"{PARAM_DB_API}/param/{param_id}/")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def _search_params(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    resp = await client.get(f"{PARAM_DB_API}/param/", params={"search": query})
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


async def _format_param(client: httpx.AsyncClient, item: dict[str, Any]) -> dict[str, Any]:
    unit_id = item.get("unit_id")
    unit = await _fetch_unit(client, unit_id) if unit_id else ""
    description = _clean_html(item.get("description", ""))
    return {
        "param_id": item.get("id"),
        "name": item.get("name", ""),
        "short_name": item.get("shortname", ""),
        "unit": unit,
        "description": description,
        "description_brief": _brief_description(description),
        "encoding": item.get("encoding_ids", []),
        "access": item.get("access_ids", []),
        "published": item.get("published"),
        "source_url": f"{PARAM_DB_WEB}/?id={item.get('id')}",
    }


async def _fetch_unit(client: httpx.AsyncClient, unit_id: int) -> str:
    try:
        resp = await client.get(f"{PARAM_DB_API}/unit/{unit_id}/")
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return ""
    return data.get("name", "") if isinstance(data, dict) else ""


def _normalize_search_text(query: str | None, short_name: str | None) -> str:
    if short_name:
        return short_name.strip()
    text = (query or "").strip()
    text = CHINESE_TO_CDS_NAME.get(text, text)
    return CDS_TO_ECMWF_SHORTNAME.get(text, text.replace("_", " "))


def _rank_results(
    results: list[dict[str, Any]],
    *,
    query: str | None,
    short_name: str | None,
) -> list[dict[str, Any]]:
    normalized_query = CHINESE_TO_CDS_NAME.get((query or "").strip(), (query or "").strip())
    target_short = (short_name or CDS_TO_ECMWF_SHORTNAME.get(normalized_query, "")).lower()
    target_name = normalized_query.replace("_", " ").lower()

    def score(item: dict[str, Any]) -> tuple[int, int]:
        short = str(item.get("shortname", "")).lower()
        name = str(item.get("name", "")).lower()
        if target_short and short == target_short:
            return (0, int(item.get("id", 10**9)))
        if target_name and name == target_name:
            return (1, int(item.get("id", 10**9)))
        if target_name and target_name in name:
            return (2, int(item.get("id", 10**9)))
        return (3, int(item.get("id", 10**9)))

    return sorted(results, key=score)


def _clean_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _brief_description(text: str, max_chars: int = 600) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."
