"""Availability helpers for ECMWF IFS data on official portal, AWS OpenData, and Google Cloud."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from meteora.adapters.ifs_adapter import (
    DEFAULT_MODEL,
    DEFAULT_STREAM,
    DEFAULT_TYPE,
    ECMWF_IFS_BASE,
    _build_grib_url,
    _build_index_url,
    _normalize_cycle,
    _normalize_date,
    _normalize_step,
)

AWS_IFS_BASE = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
AWS_REGISTRY_URL = "https://registry.opendata.aws/ecmwf-forecasts/"
GOOGLE_IFS_BASE = "https://storage.googleapis.com/ecmwf-open-data"
ECMWF_LIST_URL = f"{ECMWF_IFS_BASE}/"
AVAILABILITY_CACHE = Path.home() / ".cache" / "meteora" / "ifs_availability.json"
AWS_CACHE_TTL = timedelta(days=7)
GOOGLE_CACHE_TTL = timedelta(days=7)
ECMWF_CACHE_TTL = timedelta(minutes=30)
VALID_SOURCES = {"auto", "ecmwf", "aws", "google"}
DEFAULT_FORECAST_MAX_STEP = 240
SHORT_CYCLE_MAX_STEP = 90


@dataclass(frozen=True)
class IFSAvailabilitySummary:
    source: str
    earliest_date: str | None
    latest_date: str | None
    cycles: list[str]
    checked_at: str
    source_url: str
    notes: str | None = None


@dataclass(frozen=True)
class IFSObjectAvailability:
    source: str
    available: bool
    base_url: str
    grib_url: str
    index_url: str
    reason: str
    status_code: int | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class IFSAvailabilityDecision:
    requested_source: str
    selected_source: str | None
    available: bool
    date: str
    cycle: str
    step: int
    stream: str
    typ: str
    model: str
    ecmwf: IFSObjectAvailability
    aws: IFSObjectAvailability
    google: IFSObjectAvailability
    reason: str

    @property
    def selected(self) -> IFSObjectAvailability | None:
        if self.selected_source == "ecmwf":
            return self.ecmwf
        if self.selected_source == "aws":
            return self.aws
        if self.selected_source == "google":
            return self.google
        return None


def cache_path() -> Path:
    return AVAILABILITY_CACHE


def normalize_source(source: str | None) -> str:
    value = str(source or "auto").strip().lower()
    if value not in VALID_SOURCES:
        raise ValueError("source 只支持 auto、ecmwf、aws、google")
    return value


async def get_ifs_availability(refresh: bool = False) -> dict:
    cached = _read_cache()
    now = _now()

    ecmwf = None if refresh else _fresh_cached(cached, "ecmwf", ECMWF_CACHE_TTL, now)
    aws = None if refresh else _fresh_cached(cached, "aws", AWS_CACHE_TTL, now)

    if ecmwf is None:
        ecmwf = await _fetch_ecmwf_availability()
    if aws is None:
        aws = await _fetch_aws_availability()

    payload = {
        "ecmwf": asdict(ecmwf),
        "aws": asdict(aws),
        "cached_at": _iso(now),
    }
    _write_cache(payload)
    return payload


async def _fetch_ecmwf_availability() -> IFSAvailabilitySummary:
    checked_at = _iso(_now())
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(ECMWF_LIST_URL)
            resp.raise_for_status()
            dates = _parse_ifs_date_prefixes(resp.text)
            if not dates:
                return IFSAvailabilitySummary(
                    source="ecmwf",
                    earliest_date=None,
                    latest_date=None,
                    cycles=[],
                    checked_at=checked_at,
                    source_url=ECMWF_LIST_URL,
                    notes="官方目录暂时没有列出日期前缀。",
                )
            latest = dates[-1]
            cycles = await _fetch_ecmwf_cycles(client, latest)
            return IFSAvailabilitySummary(
                source="ecmwf",
                earliest_date=dates[0],
                latest_date=latest,
                cycles=cycles,
                checked_at=checked_at,
                source_url=ECMWF_LIST_URL,
            )
    except Exception as exc:
        return IFSAvailabilitySummary(
            source="ecmwf",
            earliest_date=None,
            latest_date=None,
            cycles=[],
            checked_at=checked_at,
            source_url=ECMWF_LIST_URL,
            notes=f"官方目录读取失败：{exc}",
        )


async def _fetch_aws_availability() -> IFSAvailabilitySummary:
    checked_at = _iso(_now())
    try:
        prefixes = await _list_aws_common_prefixes(prefix="", delimiter="/")
        dates = sorted(
            {
                match.group(1)
                for prefix in prefixes
                if (match := re.fullmatch(r"(\d{8})/", prefix))
            }
        )
        if not dates:
            return IFSAvailabilitySummary(
                source="aws",
                earliest_date=None,
                latest_date=None,
                cycles=[],
                checked_at=checked_at,
                source_url=AWS_IFS_BASE,
                notes="AWS 桶里暂时没有列出日期。",
            )
        latest = dates[-1]
        cycles = await _fetch_aws_cycles(latest)
        return IFSAvailabilitySummary(
            source="aws",
            earliest_date=dates[0],
            latest_date=latest,
            cycles=cycles,
            checked_at=checked_at,
            source_url=AWS_IFS_BASE,
            notes="日期范围来自 S3 前缀；具体产品仍会检查目标 .index 文件。",
        )
    except Exception as exc:
        return IFSAvailabilitySummary(
            source="aws",
            earliest_date=None,
            latest_date=None,
            cycles=[],
            checked_at=checked_at,
            source_url=AWS_IFS_BASE,
            notes=f"AWS 桶读取失败：{exc}",
        )


async def _fetch_ecmwf_cycles(
    client: httpx.AsyncClient, date: str
) -> list[str]:
    try:
        resp = await client.get(f"{ECMWF_IFS_BASE}/{date}/")
        resp.raise_for_status()
        return _parse_ifs_cycle_prefixes(resp.text)
    except Exception:
        return []


async def _fetch_aws_cycles(date: str) -> list[str]:
    date = _normalize_date(date)
    prefixes = await _list_aws_common_prefixes(prefix=f"{date}/", delimiter="/")
    cycles = sorted(
        {
            match.group(1)
            for prefix in prefixes
            if (match := re.fullmatch(rf"{date}/(\d{{2}})z/", prefix))
        }
    )
    return [cycle for cycle in cycles if cycle in {"00", "06", "12", "18"}]


async def resolve_ifs_source(
    *,
    date: str,
    cycle: str,
    step: int = 0,
    stream: str = DEFAULT_STREAM,
    typ: str = DEFAULT_TYPE,
    model: str = DEFAULT_MODEL,
    source: str = "auto",
) -> IFSAvailabilityDecision:
    date = _normalize_date(date)
    cycle = _normalize_cycle(cycle)
    step = _normalize_step(step)
    requested_source = normalize_source(source)

    ecmwf = _not_checked("ecmwf", date, cycle, step, stream, typ, model)
    aws = _not_checked("aws", date, cycle, step, stream, typ, model)
    google = _not_checked("google", date, cycle, step, stream, typ, model)

    if requested_source in ("auto", "ecmwf"):
        ecmwf = await _check_ifs_object(
            source="ecmwf",
            base_url=ECMWF_IFS_BASE,
            date=date,
            cycle=cycle,
            step=step,
            stream=stream,
            typ=typ,
            model=model,
        )
        if ecmwf.available:
            return IFSAvailabilityDecision(
                requested_source=requested_source,
                selected_source="ecmwf",
                available=True,
                date=date,
                cycle=cycle,
                step=step,
                stream=stream,
                typ=typ,
                model=model,
                ecmwf=ecmwf,
                aws=aws,
                google=google,
                reason="官网有这个文件。",
            )
        if requested_source == "ecmwf":
            return IFSAvailabilityDecision(
                requested_source=requested_source,
                selected_source=None,
                available=False,
                date=date,
                cycle=cycle,
                step=step,
                stream=stream,
                typ=typ,
                model=model,
                ecmwf=ecmwf,
                aws=aws,
                google=google,
                reason="官网没有这个文件。",
            )

    if requested_source in ("auto", "aws"):
        aws = await _check_ifs_object(
            source="aws",
            base_url=AWS_IFS_BASE,
            date=date,
            cycle=cycle,
            step=step,
            stream=stream,
            typ=typ,
            model=model,
        )
        if aws.available:
            reason = "AWS 历史归档有这个文件。"
            if requested_source == "auto" and not ecmwf.available:
                reason = "官网没有这个文件，已找到 AWS 历史归档。"
            return IFSAvailabilityDecision(
                requested_source=requested_source,
                selected_source="aws",
                available=True,
                date=date,
                cycle=cycle,
                step=step,
                stream=stream,
                typ=typ,
                model=model,
                ecmwf=ecmwf,
                aws=aws,
                google=google,
                reason=reason,
            )
        if requested_source == "aws":
            return IFSAvailabilityDecision(
                requested_source=requested_source,
                selected_source=None,
                available=False,
                date=date,
                cycle=cycle,
                step=step,
                stream=stream,
                typ=typ,
                model=model,
                ecmwf=ecmwf,
                aws=aws,
                google=google,
                reason="AWS 历史归档没有这个目标文件。",
            )

    if requested_source in ("auto", "google"):
        google = await _check_ifs_object(
            source="google",
            base_url=GOOGLE_IFS_BASE,
            date=date,
            cycle=cycle,
            step=step,
            stream=stream,
            typ=typ,
            model=model,
        )
        if google.available:
            reason = "Google Cloud 有这个文件。"
            if requested_source == "auto" and not aws.available:
                reason = "官网和 AWS 都没有这个文件，已找到 Google Cloud。"
            return IFSAvailabilityDecision(
                requested_source=requested_source,
                selected_source="google",
                available=True,
                date=date,
                cycle=cycle,
                step=step,
                stream=stream,
                typ=typ,
                model=model,
                ecmwf=ecmwf,
                aws=aws,
                google=google,
                reason=reason,
            )

    reason = "官网、AWS 和 Google Cloud 都没有找到这个目标文件。"
    return IFSAvailabilityDecision(
        requested_source=requested_source,
        selected_source=None,
        available=False,
        date=date,
        cycle=cycle,
        step=step,
        stream=stream,
        typ=typ,
        model=model,
        ecmwf=ecmwf,
        aws=aws,
        google=google,
        reason=reason,
    )



async def _check_ifs_object(
    *,
    source: str,
    base_url: str,
    date: str,
    cycle: str,
    step: int,
    stream: str = DEFAULT_STREAM,
    typ: str = DEFAULT_TYPE,
    model: str = DEFAULT_MODEL,
) -> IFSObjectAvailability:
    grib_url = _build_grib_url(base_url, date, cycle, step, stream, typ, model)
    index_url = _build_index_url(grib_url)
    source_url = (
        ECMWF_LIST_URL if source == "ecmwf" else
        GOOGLE_IFS_BASE if source == "google" else
        AWS_IFS_BASE
    )

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        idx_status = await _head_status(client, index_url)
        if idx_status != 200:
            return IFSObjectAvailability(
                source=source,
                available=False,
                base_url=base_url,
                grib_url=grib_url,
                index_url=index_url,
                reason=f".index 文件不可用（HTTP {idx_status or '无响应'}）。",
                status_code=idx_status,
                source_url=source_url,
            )
        grib_status = await _head_status(client, grib_url)
        if grib_status != 200:
            return IFSObjectAvailability(
                source=source,
                available=False,
                base_url=base_url,
                grib_url=grib_url,
                index_url=index_url,
                reason=f"GRIB2 文件不可用（HTTP {grib_status or '无响应'}）。",
                status_code=grib_status,
                source_url=source_url,
            )

    return IFSObjectAvailability(
        source=source,
        available=True,
        base_url=base_url,
        grib_url=grib_url,
        index_url=index_url,
        reason="目标 .index 和 GRIB2 文件都可访问。",
        status_code=200,
        source_url=source_url,
    )


async def _list_aws_common_prefixes(
    prefix: str, delimiter: str = "/"
) -> list[str]:
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
            resp = await client.get(f"{AWS_IFS_BASE}/?{params}")
            resp.raise_for_status()
            parsed = _parse_s3_list_bucket(resp.text)
            prefixes.extend(parsed["common_prefixes"])
            token = parsed["next_continuation_token"]
            if not token:
                break
    return prefixes


def _parse_ifs_date_prefixes(text: str) -> list[str]:
    dates = sorted(set(re.findall(r"/forecasts/(\d{8})/?", text)))
    if not dates:
        dates = sorted(set(re.findall(r"(\d{8})/?", text)))
    return dates


def _parse_ifs_cycle_prefixes(text: str) -> list[str]:
    cycles = sorted(set(re.findall(r"(\d{2})z/?", text)))
    return [cycle for cycle in cycles if cycle in {"00", "06", "12", "18"}]


def _parse_s3_list_bucket(xml_text: str) -> dict:
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
    step: int,
    stream: str = DEFAULT_STREAM,
    typ: str = DEFAULT_TYPE,
    model: str = DEFAULT_MODEL,
) -> IFSObjectAvailability:
    if source == "ecmwf":
        base_url = ECMWF_IFS_BASE
        source_url = ECMWF_LIST_URL
    elif source == "google":
        base_url = GOOGLE_IFS_BASE
        source_url = GOOGLE_IFS_BASE
    else:
        base_url = AWS_IFS_BASE
        source_url = AWS_IFS_BASE
    grib_url = _build_grib_url(base_url, date, cycle, step, stream, typ, model)
    return IFSObjectAvailability(
        source=source,
        available=False,
        base_url=base_url,
        grib_url=grib_url,
        index_url=_build_index_url(grib_url),
        reason="未检查。",
        source_url=source_url,
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
) -> IFSAvailabilitySummary | None:
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
        return IFSAvailabilitySummary(**item)
    except TypeError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
