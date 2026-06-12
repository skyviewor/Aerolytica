"""GFS/NCEP GRIB2 parameter lookup helpers."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import structlog

logger = structlog.get_logger()

NCO_GRIB2_BASE = "https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/"
TABLE_4_2_INDEX = urljoin(NCO_GRIB2_BASE, "grib2_table4-2.shtml")
CACHE_DIR = Path.home() / ".cache" / "aero"
CACHE_FILE = CACHE_DIR / "gfs_parameters.json"
CACHE_TTL = timedelta(hours=24)

CHINESE_ALIASES = {
    "温度": ["TMP", "TMAX", "TMIN", "DPT"],
    "气温": ["TMP", "TMAX", "TMIN"],
    "露点": ["DPT"],
    "降水": ["APCP", "PRATE", "CPRAT", "TCDC"],
    "总降水": ["APCP"],
    "风": ["UGRD", "VGRD", "GUST"],
    "纬向风": ["UGRD"],
    "经向风": ["VGRD"],
    "位势高度": ["HGT"],
    "高度": ["HGT"],
    "湿度": ["RH", "SPFH"],
    "相对湿度": ["RH"],
    "比湿": ["SPFH"],
    "气压": ["PRES", "PRMSL"],
    "海平面气压": ["PRMSL"],
    "云": ["TCDC", "LCDC", "MCDC", "HCDC"],
    "云量": ["TCDC", "LCDC", "MCDC", "HCDC"],
}


async def get_gfs_parameters(force_refresh: bool = False) -> list[dict[str, Any]]:
    cached = _load_cache(ignore_ttl=force_refresh)
    if cached is not None and not force_refresh:
        return cached

    try:
        params = await fetch_gfs_parameters()
    except Exception as exc:
        logger.warning("gfs parameter fetch failed", error=str(exc))
        cached = _load_cache(ignore_ttl=True)
        if cached is not None:
            return cached
        raise RuntimeError(f"NCEP GRIB2 参数表抓取失败，且本地无缓存：{exc}") from exc

    if params:
        _save_cache(params)
        return params

    cached = _load_cache(ignore_ttl=True)
    if cached is not None:
        return cached
    raise RuntimeError("NCEP GRIB2 参数表没有解析出参数，且本地无缓存")


async def fetch_gfs_parameters() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        index_resp = await client.get(TABLE_4_2_INDEX)
        index_resp.raise_for_status()
        links = _extract_table_links(index_resp.text)

        parameters: list[dict[str, Any]] = []
        seen: set[tuple[int, int, int, str]] = set()
        for link in links:
            url = urljoin(TABLE_4_2_INDEX, link)
            match = re.search(r"grib2_table4-2-(\d+)-(\d+)\.shtml", link)
            if not match:
                continue
            discipline = int(match.group(1))
            category = int(match.group(2))
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except Exception as exc:
                logger.warning("gfs parameter page failed", url=url, error=str(exc))
                continue

            for item in _parse_parameter_page(resp.text, discipline, category, url):
                key = (item["discipline"], item["category"], item["number"], item["abbrev"])
                if key in seen:
                    continue
                seen.add(key)
                parameters.append(item)
        return parameters


def search_gfs_parameters(
    parameters: list[dict[str, Any]],
    keyword: str | None = None,
) -> list[dict[str, Any]]:
    if not keyword:
        return parameters

    kw = keyword.strip()
    aliases = set()
    for alias, abbrevs in CHINESE_ALIASES.items():
        if kw in alias or alias in kw:
            aliases.update(abbrevs)
    kw_lower = kw.lower()
    kw_upper = kw.upper()

    results = []
    for item in parameters:
        abbrev = str(item.get("abbrev", "")).upper()
        parameter = str(item.get("parameter", "")).lower()
        units = str(item.get("units", "")).lower()
        if (
            kw_upper == abbrev
            or abbrev in aliases
            or kw_lower in parameter
            or kw_lower in units
            or kw_lower in abbrev.lower()
        ):
            results.append(item)
    return _rank_parameters(results, keyword=keyword, abbrev=None)


async def lookup_gfs_parameters(
    query: str | None = None,
    *,
    abbrev: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    if not query and not abbrev:
        return {
            "found": False,
            "message": "query 和 abbrev 至少提供一个。",
            "source": TABLE_4_2_INDEX,
        }

    params = await get_gfs_parameters()
    search_text = abbrev or query or ""
    results = search_gfs_parameters(params, search_text)
    results = _rank_parameters(results, keyword=query, abbrev=abbrev)[: max(1, limit)]
    return {
        "found": bool(results),
        "query": query or "",
        "abbrev": (abbrev or "").upper(),
        "count": len(results),
        "parameters": results,
        "source": TABLE_4_2_INDEX,
        "note": "来源为 NCEP/NCO GRIB2 Table 4.2，适合核对 GFS GRIB short name、单位和官方含义。",
    }


def _load_cache(ignore_ttl: bool = False) -> list[dict[str, Any]] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        with CACHE_FILE.open() as f:
            cached = json.load(f)
        cached_at = datetime.fromisoformat(cached["cached_at"])
        if not ignore_ttl and datetime.now() - cached_at > CACHE_TTL:
            return None
        return cached["parameters"]
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _save_cache(parameters: list[dict[str, Any]]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached_at = datetime.now().isoformat()
    enriched = [{**item, "cached_at": cached_at} for item in parameters]
    with CACHE_FILE.open("w") as f:
        json.dump(
            {"cached_at": cached_at, "count": len(enriched), "parameters": enriched},
            f,
            ensure_ascii=False,
            indent=2,
        )


def _extract_table_links(index_html: str) -> list[str]:
    parser = _LinkParser()
    parser.feed(index_html)
    links = []
    seen = set()
    for href in parser.hrefs:
        if not re.fullmatch(r"grib2_table4-2-\d+-\d+\.shtml", href):
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append(href)
    return links


def _parse_parameter_page(
    page_html: str,
    discipline: int,
    category: int,
    source_url: str,
) -> list[dict[str, Any]]:
    parser = _TableParser()
    parser.feed(page_html)
    category_label = _extract_category_label(page_html)
    params = []
    for row in parser.rows:
        if len(row) < 4:
            continue
        number_text, parameter, units, abbrev = row[:4]
        if not re.fullmatch(r"\d+", number_text.strip()):
            continue
        abbrev = abbrev.strip().upper()
        if not abbrev or abbrev in {"-", "RESERVED", "MISSING"}:
            continue
        params.append(
            {
                "discipline": discipline,
                "category": category,
                "number": int(number_text),
                "abbrev": abbrev,
                "parameter": parameter.strip(),
                "units": units.strip(),
                "category_label": category_label,
                "source_url": source_url,
            }
        )
    return params


def _extract_category_label(page_html: str) -> str:
    match = re.search(r"\(Meteorological products,\s*([^)]+?)\s*category\)", page_html, re.I | re.S)
    if not match:
        return ""
    return _clean_text(match.group(1))


def _rank_parameters(
    results: list[dict[str, Any]],
    *,
    keyword: str | None,
    abbrev: str | None,
) -> list[dict[str, Any]]:
    target_abbrev = (abbrev or keyword or "").strip().upper()
    target_text = (keyword or abbrev or "").strip().lower()

    def score(item: dict[str, Any]) -> tuple[int, int, int]:
        item_abbrev = str(item.get("abbrev", "")).upper()
        item_name = str(item.get("parameter", "")).lower()
        if target_abbrev and item_abbrev == target_abbrev:
            return (0, int(item.get("discipline", 999)), int(item.get("number", 999)))
        if target_text and item_name == target_text:
            return (1, int(item.get("discipline", 999)), int(item.get("number", 999)))
        if target_text and target_text in item_name:
            return (2, int(item.get("discipline", 999)), int(item.get("number", 999)))
        return (3, int(item.get("discipline", 999)), int(item.get("number", 999)))

    return sorted(results, key=score)


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._in_row = False
        self._in_cell = False
        self._row: list[str] = []
        self._cell: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._in_row = True
            self._row = []
        elif tag in {"td", "th"} and self._in_row:
            self._in_cell = True
            self._cell = []
        elif tag == "br" and self._in_cell:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._in_cell:
            self._row.append(_clean_text("".join(self._cell)))
            self._in_cell = False
            self._cell = []
        elif tag == "tr" and self._in_row:
            if self._row:
                self.rows.append(self._row)
            self._in_row = False
            self._row = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell.append(data)
