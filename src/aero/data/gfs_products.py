"""NCO GFS product inventory helpers."""

from __future__ import annotations

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

GFS_PRODUCTS_PAGE = "https://www.nco.ncep.noaa.gov/pmb/products/gfs/"
CACHE_DIR = Path.home() / ".cache" / "aero"
CACHE_FILE = CACHE_DIR / "gfs_product_inventory.json"
CACHE_TTL = timedelta(hours=24)


async def get_gfs_product_inventory(force_refresh: bool = False) -> dict[str, Any]:
    cached = _load_cache(ignore_ttl=force_refresh)
    if cached is not None and not force_refresh:
        return cached

    try:
        inventory = await fetch_gfs_product_inventory()
    except Exception as exc:
        logger.warning("gfs product inventory fetch failed", error=str(exc))
        cached = _load_cache(ignore_ttl=True)
        if cached is not None:
            return cached
        raise RuntimeError(f"NCO GFS 产品清单抓取失败，且本地无缓存：{exc}") from exc

    _save_cache(inventory)
    return inventory


async def fetch_gfs_product_inventory() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(GFS_PRODUCTS_PAGE)
        resp.raise_for_status()
        links = _extract_inventory_links(resp.text)

        products = []
        records = []
        for link in links:
            url = urljoin(GFS_PRODUCTS_PAGE, link)
            meta = product_meta_from_inventory_href(link)
            if not meta:
                continue
            products.append({**meta, "inventory_url": url})
            try:
                item_resp = await client.get(url)
                item_resp.raise_for_status()
            except Exception as exc:
                logger.warning("gfs inventory page failed", url=url, error=str(exc))
                continue
            records.extend(parse_inventory_page(item_resp.text, url, meta))

    now = datetime.now().isoformat()
    return {
        "source_url": GFS_PRODUCTS_PAGE,
        "cached_at": now,
        "product_count": len(products),
        "record_count": len(records),
        "products": products,
        "records": records,
    }


def search_gfs_inventory(
    inventory: dict[str, Any],
    keyword: str | None = None,
) -> list[dict[str, Any]]:
    records = list(inventory.get("records", []))
    if not keyword:
        return records

    kw = keyword.strip()
    kw_lower = kw.lower()
    kw_upper = kw.upper()
    results = []
    for record in records:
        haystack = " ".join(
            str(record.get(field, ""))
            for field in (
                "parameter",
                "description",
                "level",
                "forecast_valid",
                "product",
                "file_name",
                "resolution",
                "subset",
            )
        ).lower()
        if kw_upper == str(record.get("parameter", "")).upper() or kw_lower in haystack:
            results.append(record)
    return sorted(
        results,
        key=lambda r: (r.get("product", ""), r.get("file_name", ""), r.get("number", 0)),
    )


def product_meta_from_inventory_href(href: str) -> dict[str, Any] | None:
    filename = Path(href).name.removesuffix(".shtml")
    match = re.fullmatch(r"gfs\.t00z\.(?P<product>.+)\.(?P<token>anl|f\d{3})", filename)
    if match:
        product = match.group("product")
        token = match.group("token")
    else:
        match = re.fullmatch(r"gfs\.t00z\.(?P<product>[a-z0-9]+)f(?P<hour>\d{3})\.grib2", filename)
        if not match:
            return None
        product = match.group("product")
        token = f"f{match.group('hour')}"
    if not product or not token:
        return None
    resolution = _resolution_from_product(product)
    subset = _subset_from_product(product)
    return {
        "file_name": filename,
        "product": product,
        "resolution": resolution,
        "subset": subset,
        "forecast_token": token,
        "forecast_hour": 0 if token == "anl" else int(token[1:]),
        "is_analysis_inventory": token == "anl",
    }


def parse_inventory_page(
    page_html: str,
    source_url: str,
    product_meta: dict[str, Any],
) -> list[dict[str, Any]]:
    parser = _TableParser()
    parser.feed(page_html)
    records = []
    for row in parser.rows:
        if len(row) < 5 or not re.fullmatch(r"\d+", row[0].strip()):
            continue
        records.append(
            {
                **product_meta,
                "number": int(row[0]),
                "level": row[1].strip(),
                "parameter": row[2].strip().upper(),
                "forecast_valid": row[3].strip(),
                "description": row[4].strip(),
                "source_url": source_url,
            }
        )
    return records


def _extract_inventory_links(page_html: str) -> list[str]:
    parser = _LinkParser()
    parser.feed(page_html)
    seen = set()
    links = []
    for href in parser.hrefs:
        name = Path(href).name
        supported = (
            re.fullmatch(r"gfs\.t00z\..+?\.(?:anl|f\d{3})\.shtml", name)
            or re.fullmatch(r"gfs\.t00z\.[a-z0-9]+f\d{3}\.grib2\.shtml", name)
        )
        if not supported:
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append(href)
    return sorted(links)


def _load_cache(ignore_ttl: bool = False) -> dict[str, Any] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        with CACHE_FILE.open() as f:
            cached = json.load(f)
        cached_at = datetime.fromisoformat(cached["cached_at"])
        if not ignore_ttl and datetime.now() - cached_at > CACHE_TTL:
            return None
        return cached
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _save_cache(inventory: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with CACHE_FILE.open("w") as f:
        json.dump(inventory, f, ensure_ascii=False, indent=2)


def _resolution_from_product(product: str) -> str:
    match = re.search(r"\.(\d+p\d+)", product)
    if not match:
        return ""
    token = match.group(1)
    return token.replace("p", ".") + " degree"


def _subset_from_product(product: str) -> str:
    prefix = product.split(".", 1)[0]
    labels = {
        "pgrb2": "most commonly used parameters",
        "pgrb2b": "least commonly used parameters",
        "sfluxgrb": "surface flux fields",
    }
    return labels.get(prefix, prefix)


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


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
