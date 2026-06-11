"""Availability helpers for GEFS data on NOMADS and AWS OpenData.

GEFS (Global Ensemble Forecast System) uses NOMADS at `/gens/prod/` and
AWS OpenData bucket `noaa-gefs-bdp-pds`. The architecture mirrors GFS
availability but with ensemble member dimensions and 840h forecast range.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from meteora.adapters.gefs_adapter import (
    DEFAULT_PRODUCT,
    NOMADS_GEFS_BASE,
    _get_product_info,
    _member_prefix,
    normalize_cycle,
    normalize_date,
    normalize_forecast_hour,
    normalize_member,
    normalize_product,
)

AWS_GEFS_BASE = "https://noaa-gefs-bdp-pds.s3.amazonaws.com"
NOMADS_LIST_URL = f"{NOMADS_GEFS_BASE}/"
AWS_REGISTRY_URL = "https://registry.opendata.aws/noaa-gefs-bdp-pds/"
AVAILABILITY_CACHE = Path.home() / ".cache" / "meteora" / "gefs_availability.json"
AWS_CACHE_TTL = timedelta(days=7)
NOMADS_CACHE_TTL = timedelta(minutes=30)
VALID_SOURCES = {"auto", "nomads", "aws"}
MAX_FORECAST_HOUR = 840
GEFS_0P50_FORECAST_SEGMENTS = (
    {"start": 0, "end": 240, "step": 3, "label": "0-240 小时每 3 小时"},
    {"start": 246, "end": 840, "step": 6, "label": "246-840 小时每 6 小时"},
)
GEFS_0P25_FORECAST_SEGMENTS = (
    {"start": 0, "end": 120, "step": 3, "label": "0-120 小时每 3 小时"},
    {"start": 126, "end": 840, "step": 6, "label": "126-840 小时每 6 小时"},
)


@dataclass(frozen=True)
class GEFSAvailabilitySummary:
    source: str
    earliest_date: str | None
    latest_date: str | None
    cycles: list[str]
    checked_at: str
    source_url: str
    notes: str | None = None


@dataclass(frozen=True)
class GEFSObjectAvailability:
    source: str
    available: bool
    base_url: str
    grib_url: str
    idx_url: str
    reason: str
    status_code: int | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class GEFSAvailabilityDecision:
    requested_source: str
    selected_source: str | None
    available: bool
    date: str
    cycle: str
    product: str
    forecast_hour: int
    member: str
    nomads: GEFSObjectAvailability
    aws: GEFSObjectAvailability
    reason: str

    @property
    def selected(self) -> GEFSObjectAvailability | None:
        if self.selected_source == "nomads":
            return self.nomads
        if self.selected_source == "aws":
            return self.aws
        return None


def gefs_forecast_hour_segments(
    product: str = DEFAULT_PRODUCT,
    date: str | None = None,
) -> list[dict]:
    product = normalize_product(product)
    date = normalize_date(date) if date else None
    segments = _forecast_segments_for_product(product)
    return [
        {
            **segment,
            "product": product,
            "date": date,
        }
        for segment in segments
    ]


def gefs_forecast_hours_for_range(
    *,
    start_hour: int = 0,
    end_hour: int | None = None,
    duration_hours: int | None = None,
    product: str = DEFAULT_PRODUCT,
    date: str | None = None,
) -> dict:
    product = normalize_product(product)
    date = normalize_date(date) if date else None
    start_hour = normalize_forecast_hour(start_hour)
    if duration_hours is not None:
        if duration_hours < 0:
            raise ValueError("duration_hours 不能为负数")
        resolved_end = start_hour + int(duration_hours)
    elif end_hour is not None:
        resolved_end = normalize_forecast_hour(end_hour)
    else:
        resolved_end = start_hour
    if resolved_end < start_hour:
        raise ValueError("end_hour 不能小于 start_hour")
    if resolved_end > MAX_FORECAST_HOUR:
        raise ValueError(f"GEFS forecast hour 目前最多支持到 {MAX_FORECAST_HOUR}")

    hours = [
        hour
        for hour in range(start_hour, resolved_end + 1)
        if is_gefs_forecast_hour_available(hour, product=product)
    ]
    requested_hours = list(range(start_hour, resolved_end + 1))
    unavailable_hours = [hour for hour in requested_hours if hour not in hours]
    intervals = sorted({b - a for a, b in zip(hours, hours[1:], strict=False)})

    return {
        "product": product,
        "date": date,
        "start_hour": start_hour,
        "end_hour": resolved_end,
        "forecast_hours": hours,
        "count": len(hours),
        "intervals": intervals,
        "unavailable_hours": unavailable_hours,
        "segments": gefs_forecast_hour_segments(product, date=date),
        "note": _forecast_schedule_note(product),
        "availability_check_recommended": True,
    }


def is_gefs_forecast_hour_available(
    hour: int,
    product: str = DEFAULT_PRODUCT,
) -> bool:
    hour = normalize_forecast_hour(hour)
    product = normalize_product(product)
    for segment in _forecast_segments_for_product(product):
        if segment["start"] <= hour <= segment["end"]:
            return (hour - segment["start"]) % segment["step"] == 0
    return False


def _forecast_segments_for_product(product: str) -> tuple[dict, ...]:
    product = normalize_product(product)
    if ".0p25" in product:
        return GEFS_0P25_FORECAST_SEGMENTS
    return GEFS_0P50_FORECAST_SEGMENTS


def _forecast_schedule_note(product: str) -> str:
    segments = _forecast_segments_for_product(product)
    label = "、".join(seg["label"] for seg in segments)
    return (
        f"GEFS 集合预报时效规则：{label}。"
        f"共有 31 个成员（控制 c00 + 扰动 p01-p30），默认只下载控制成员。"
    )


def cache_path() -> Path:
    return AVAILABILITY_CACHE


def normalize_source(source: str | None) -> str:
    value = str(source or "auto").strip().lower()
    if value not in VALID_SOURCES:
        raise ValueError("source 只支持 auto、nomads、aws")
    return value


async def get_gefs_availability(refresh: bool = False) -> dict:
    cached = _read_cache()
    now = _now()

    nomads = None if refresh else _fresh_cached(cached, "nomads", NOMADS_CACHE_TTL, now)
    aws = None if refresh else _fresh_cached(cached, "aws", AWS_CACHE_TTL, now)

    if nomads is None:
        nomads = await fetch_nomads_availability()
    if aws is None:
        aws = await fetch_aws_availability()

    payload = {
        "nomads": asdict(nomads),
        "aws": asdict(aws),
        "cached_at": _iso(now),
    }
    _write_cache(payload)
    return payload


async def fetch_nomads_availability() -> GEFSAvailabilitySummary:
    checked_at = _iso(_now())
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(NOMADS_LIST_URL)
            resp.raise_for_status()
            dates = parse_gefs_date_prefixes(resp.text)
            if not dates:
                return GEFSAvailabilitySummary(
                    source="nomads",
                    earliest_date=None,
                    latest_date=None,
                    cycles=[],
                    checked_at=checked_at,
                    source_url=NOMADS_LIST_URL,
                    notes="官网目录暂时没有列出 gefs.YYYYMMDD 日期。",
                )
            latest = dates[-1]
            cycles = await _fetch_nomads_cycles(client, latest)
            return GEFSAvailabilitySummary(
                source="nomads",
                earliest_date=dates[0],
                latest_date=latest,
                cycles=cycles,
                checked_at=checked_at,
                source_url=NOMADS_LIST_URL,
            )
    except Exception as exc:
        return GEFSAvailabilitySummary(
            source="nomads",
            earliest_date=None,
            latest_date=None,
            cycles=[],
            checked_at=checked_at,
            source_url=NOMADS_LIST_URL,
            notes=f"官网目录读取失败：{exc}",
        )


async def fetch_aws_availability() -> GEFSAvailabilitySummary:
    checked_at = _iso(_now())
    try:
        prefixes = await list_aws_common_prefixes(prefix="gefs.", delimiter="/")
        dates = sorted(
            {
                match.group(1)
                for prefix in prefixes
                if (match := re.fullmatch(r"gefs\.(\d{8})/", prefix))
            }
        )
        if not dates:
            return GEFSAvailabilitySummary(
                source="aws",
                earliest_date=None,
                latest_date=None,
                cycles=[],
                checked_at=checked_at,
                source_url=AWS_GEFS_BASE,
                notes="AWS 桶里暂时没有列出 gefs.YYYYMMDD 日期。",
            )
        latest = dates[-1]
        cycles = await fetch_aws_cycles(latest)
        return GEFSAvailabilitySummary(
            source="aws",
            earliest_date=dates[0],
            latest_date=latest,
            cycles=cycles,
            checked_at=checked_at,
            source_url=AWS_GEFS_BASE,
            notes="日期范围来自 S3 前缀；具体产品仍会检查目标 .idx 文件。",
        )
    except Exception as exc:
        return GEFSAvailabilitySummary(
            source="aws",
            earliest_date=None,
            latest_date=None,
            cycles=[],
            checked_at=checked_at,
            source_url=AWS_GEFS_BASE,
            notes=f"AWS 桶读取失败：{exc}",
        )


async def fetch_aws_cycles(date: str) -> list[str]:
    date = normalize_date(date)
    prefixes = await list_aws_common_prefixes(prefix=f"gefs.{date}/", delimiter="/")
    cycles = sorted(
        {
            match.group(1)
            for prefix in prefixes
            if (match := re.fullmatch(rf"gefs\.{date}/(\d{{2}})/", prefix))
        }
    )
    return [cycle for cycle in cycles if cycle in {"00", "06", "12", "18"}]


async def resolve_gefs_source(
    *,
    date: str,
    cycle: str,
    product: str = DEFAULT_PRODUCT,
    forecast_hour: int = 0,
    member: str = "c00",
    source: str = "auto",
) -> GEFSAvailabilityDecision:
    date = normalize_date(date)
    cycle = normalize_cycle(cycle)
    product = normalize_product(product)
    forecast_hour = normalize_forecast_hour(forecast_hour)
    member = normalize_member(member)
    requested_source = normalize_source(source)

    nomads = _not_checked("nomads", date, cycle, forecast_hour, product, member)
    aws = _not_checked("aws", date, cycle, forecast_hour, product, member)

    if requested_source in ("auto", "nomads"):
        nomads = await check_gefs_object(
            source="nomads",
            base_url=NOMADS_GEFS_BASE,
            date=date,
            cycle=cycle,
            forecast_hour=forecast_hour,
            product=product,
            member=member,
        )
        if nomads.available:
            return GEFSAvailabilityDecision(
                requested_source=requested_source,
                selected_source="nomads",
                available=True,
                date=date,
                cycle=cycle,
                product=product,
                forecast_hour=forecast_hour,
                member=member,
                nomads=nomads,
                aws=aws,
                reason="官网有这个文件。",
            )
        if requested_source == "nomads":
            return GEFSAvailabilityDecision(
                requested_source=requested_source,
                selected_source=None,
                available=False,
                date=date,
                cycle=cycle,
                product=product,
                forecast_hour=forecast_hour,
                member=member,
                nomads=nomads,
                aws=aws,
                reason="官网没有这个文件。",
            )

    if requested_source in ("auto", "aws"):
        aws = await check_gefs_object(
            source="aws",
            base_url=AWS_GEFS_BASE,
            date=date,
            cycle=cycle,
            forecast_hour=forecast_hour,
            product=product,
            member=member,
        )
        if aws.available:
            reason = "AWS 历史归档有这个文件。"
            if requested_source == "auto" and not nomads.available:
                reason = "官网没有这个文件，已找到 AWS 历史归档。"
            return GEFSAvailabilityDecision(
                requested_source=requested_source,
                selected_source="aws",
                available=True,
                date=date,
                cycle=cycle,
                product=product,
                forecast_hour=forecast_hour,
                member=member,
                nomads=nomads,
                aws=aws,
                reason=reason,
            )

    reason = "官网和 AWS 历史归档都没有找到这个目标文件。"
    if requested_source == "aws":
        reason = "AWS 历史归档没有这个目标文件。"
    return GEFSAvailabilityDecision(
        requested_source=requested_source,
        selected_source=None,
        available=False,
        date=date,
        cycle=cycle,
        product=product,
        forecast_hour=forecast_hour,
        member=member,
        nomads=nomads,
        aws=aws,
        reason=reason,
    )


async def check_gefs_object(
    *,
    source: str,
    base_url: str,
    date: str,
    cycle: str,
    forecast_hour: int,
    product: str,
    member: str = "c00",
) -> GEFSObjectAvailability:
    grib_url = build_gefs_grib_url(base_url, date, cycle, forecast_hour, product, member)
    idx_url = f"{grib_url}.idx"
    source_url = NOMADS_LIST_URL if source == "nomads" else AWS_GEFS_BASE

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        idx_status = await _head_status(client, idx_url)
        if idx_status != 200:
            return GEFSObjectAvailability(
                source=source,
                available=False,
                base_url=base_url,
                grib_url=grib_url,
                idx_url=idx_url,
                reason=f".idx 文件不可用（HTTP {idx_status or '无响应'}）。",
                status_code=idx_status,
                source_url=source_url,
            )
        grib_status = await _head_status(client, grib_url)
        if grib_status != 200:
            return GEFSObjectAvailability(
                source=source,
                available=False,
                base_url=base_url,
                grib_url=grib_url,
                idx_url=idx_url,
                reason=f"GRIB2 文件不可用（HTTP {grib_status or '无响应'}）。",
                status_code=grib_status,
                source_url=source_url,
            )

    return GEFSObjectAvailability(
        source=source,
        available=True,
        base_url=base_url,
        grib_url=grib_url,
        idx_url=idx_url,
        reason="目标 .idx 和 GRIB2 文件都可访问。",
        status_code=200,
        source_url=source_url,
    )


def build_gefs_grib_url(
    base_url: str,
    date: str,
    cycle: str,
    forecast_hour: int,
    product: str = DEFAULT_PRODUCT,
    member: str = "c00",
) -> str:
    prod_info = _get_product_info(normalize_product(product))
    prefix = _member_prefix(normalize_member(member))
    return (
        f"{base_url.rstrip('/')}/gefs.{normalize_date(date)}/{normalize_cycle(cycle)}/atmos/"
        f"{prod_info['dir']}/{prefix}.t{normalize_cycle(cycle)}z."
        f"{prod_info['code']}.{prod_info['resolution']}.f{normalize_forecast_hour(forecast_hour):03d}"
    )


async def list_aws_common_prefixes(prefix: str, delimiter: str = "/") -> list[str]:
    prefixes: list[str] = []
    token: str | None = None
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        while True:
            params = (
                f"list-type=2&delimiter={quote(delimiter, safe='')}"
                f"&prefix={quote(prefix, safe='')}&max-keys=1000"
            )
            if token:
                params += f"&continuation-token={quote(token, safe='')}"
            resp = await client.get(f"{AWS_GEFS_BASE}/?{params}")
            resp.raise_for_status()
            parsed = parse_s3_list_bucket(resp.text)
            prefixes.extend(parsed["common_prefixes"])
            token = parsed["next_continuation_token"]
            if not token:
                break
    return prefixes


def parse_gefs_date_prefixes(text: str) -> list[str]:
    dates = sorted(set(re.findall(r"gefs\.(\d{8})/?", text)))
    return dates


def parse_gefs_cycle_prefixes(text: str) -> list[str]:
    cycles = sorted(set(re.findall(r'href=["\']?(\d{2})/?["\']?', text, re.IGNORECASE)))
    return [cycle for cycle in cycles if cycle in {"00", "06", "12", "18"}]


def parse_s3_list_bucket(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    common_prefixes = [
        elem.text or ""
        for elem in root.findall("s3:CommonPrefixes/s3:Prefix", ns)
        if elem.text
    ]
    contents = [
        elem.text or ""
        for elem in root.findall("s3:Contents/s3:Key", ns)
        if elem.text
    ]
    token = root.findtext("s3:NextContinuationToken", default=None, namespaces=ns)
    return {
        "common_prefixes": common_prefixes,
        "contents": contents,
        "next_continuation_token": token,
    }


async def _fetch_nomads_cycles(client: httpx.AsyncClient, date: str) -> list[str]:
    try:
        resp = await client.get(f"{NOMADS_GEFS_BASE}/gefs.{date}/")
        resp.raise_for_status()
        return parse_gefs_cycle_prefixes(resp.text)
    except Exception:
        return []


async def _head_status(client: httpx.AsyncClient, url: str) -> int | None:
    try:
        resp = await client.head(url)
        return resp.status_code
    except Exception:
        return None


def _not_checked(
    source: str,
    date: str,
    cycle: str,
    forecast_hour: int,
    product: str,
    member: str = "c00",
) -> GEFSObjectAvailability:
    base_url = NOMADS_GEFS_BASE if source == "nomads" else AWS_GEFS_BASE
    grib_url = build_gefs_grib_url(base_url, date, cycle, forecast_hour, product, member)
    return GEFSObjectAvailability(
        source=source,
        available=False,
        base_url=base_url,
        grib_url=grib_url,
        idx_url=f"{grib_url}.idx",
        reason="未检查。",
        source_url=NOMADS_LIST_URL if source == "nomads" else AWS_GEFS_BASE,
    )


def _read_cache() -> dict:
    try:
        return json.loads(AVAILABILITY_CACHE.read_text())
    except Exception:
        return {}


def _write_cache(payload: dict) -> None:
    try:
        AVAILABILITY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        AVAILABILITY_CACHE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _fresh_cached(
    cached: dict, key: str, ttl: timedelta, now: datetime
) -> GEFSAvailabilitySummary | None:
    item = cached.get(key)
    if not isinstance(item, dict):
        return None
    checked_at = item.get("checked_at")
    if not checked_at:
        return None
    if item.get("earliest_date") is None and item.get("latest_date") is None:
        return None
    try:
        checked = datetime.fromisoformat(checked_at)
    except ValueError:
        return None
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    if now - checked > ttl:
        return None
    try:
        return GEFSAvailabilitySummary(**item)
    except TypeError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
