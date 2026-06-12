"""GEFS adapter using NOMADS GRIB2 .idx files and HTTP Range requests.

GEFS (Global Ensemble Forecast System) stores all ensemble members in the same
product directory, differentiated by filename prefix:
  geavg = ensemble average, gec00 = control, gep01-p30 = perturbed
  gespr = ensemble spread

NOMADS path pattern:
  {base}/gefs.{date}/{cycle}/atmos/{prod_dir}/{prefix}.t{cycle}z.{prod_code}.{res}.f{hour}
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from aero.adapters.cds_adapter import config_output_dir
from aero.agent.progress import cancel_requested, emit_progress

CHUNK_SIZE = 5 * 1024 * 1024
DEFAULT_PRODUCT = "gefs.0p50"
DATASET_ID = "gefs-pgrb2-0p50"
DEFAULT_MEMBERS = ("c00",)
NOMADS_GEFS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod"
VALID_CYCLES = {"00", "06", "12", "18"}
MAX_FORECAST_HOUR = 840

_PRODUCT_MAP = {
    "gefs.0p50": {
        "dir": "pgrb2ap5",
        "code": "pgrb2a",
        "resolution": "0p50",
        "description": "0.5° pressure-level + surface fields (TMP at 2m/500mb, HGT, UGRD, VGRD, etc.)",
    },
    "gefs.0p50b": {
        "dir": "pgrb2bp5",
        "code": "pgrb2b",
        "resolution": "0p50",
        "description": "0.5° surface & bias-corrected fields (TMP 2m, RH 2m, PRMSL, etc.)",
    },
    "gefs.0p25": {
        "dir": "pgrb2sp25",
        "code": "pgrb2s",
        "resolution": "0p25",
        "description": "0.25° pressure-level fields (finest resolution, limited date range)",
    },
}


@dataclass(frozen=True)
class GEFSIndexEntry:
    index: int
    start_byte: int
    end_byte: int | None
    member: str
    variable: str
    level: str
    forecast: str
    raw: str

    @property
    def range_header(self) -> str:
        if self.end_byte is None:
            return f"bytes={self.start_byte}-"
        return f"bytes={self.start_byte}-{self.end_byte}"

    @property
    def byte_count(self) -> int | None:
        if self.end_byte is None:
            return None
        return self.end_byte - self.start_byte + 1


@dataclass(frozen=True)
class GEFSDownloadFile:
    member: str
    forecast_hour: int
    idx_url: str
    grib_url: str
    file_path: Path
    selected_entries: list[GEFSIndexEntry]
    missing: list[dict]
    downloaded_bytes: int
    source: str = "nomads"


class GEFSAdapter:
    """Download selected GEFS GRIB2 messages from NOMADS official files."""

    def __init__(self, base_url: str = NOMADS_GEFS_BASE):
        self._base_url = base_url.rstrip("/")

    async def download(
        self,
        *,
        date: str,
        cycle: str,
        forecast_hours: list[int],
        variables: list[str],
        members: list[str] | None = None,
        product: str = DEFAULT_PRODUCT,
        levels: list[str] | None = None,
        dest_dir: Path | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[GEFSDownloadFile]:
        date = normalize_date(date)
        cycle = normalize_cycle(cycle)
        forecast_hours = normalize_forecast_hours(forecast_hours)
        variables = normalize_variables(variables)
        members = normalize_members(members)
        product = normalize_product(product)
        levels = normalize_levels(levels)
        dest_root = dest_dir or config_output_dir()
        dest_root.mkdir(parents=True, exist_ok=True)

        results: list[GEFSDownloadFile] = []
        for member in members:
            for fhour in forecast_hours:
                if cancel_requested():
                    raise RuntimeError("下载已取消")
                result = await self.download_one(
                    date=date,
                    cycle=cycle,
                    forecast_hour=fhour,
                    variables=variables,
                    member=member,
                    product=product,
                    levels=levels,
                    dest_dir=dest_root,
                    on_progress=on_progress,
                )
                results.append(result)
        return results

    async def download_one(
        self,
        *,
        date: str,
        cycle: str,
        forecast_hour: int,
        variables: list[str],
        member: str = "c00",
        product: str = DEFAULT_PRODUCT,
        levels: list[str] | None = None,
        dest_dir: Path | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> GEFSDownloadFile:
        date = normalize_date(date)
        cycle = normalize_cycle(cycle)
        forecast_hour = normalize_forecast_hour(forecast_hour)
        variables = normalize_variables(variables)
        member = normalize_member(member)
        product = normalize_product(product)
        levels = normalize_levels(levels)
        dest_root = dest_dir or config_output_dir()
        dest_root.mkdir(parents=True, exist_ok=True)

        grib_url = self.build_grib_url(date, cycle, forecast_hour, product, member)
        idx_url = f"{grib_url}.idx"
        emit_progress(
            f"正在读取 GEFS 索引：{date} {cycle}Z f{forecast_hour:03d} "
            f"成员={member}，变量={', '.join(variables)}"
        )

        idx_text = await asyncio.to_thread(self._fetch_text, idx_url)
        entries = parse_gefs_idx(idx_text)
        selected, missing = select_gefs_entries(entries, variables, levels, member)
        if not selected:
            available = _summarize_available(entries)
            raise RuntimeError(
                f"GEFS .idx 中没有找到匹配字段。"
                f"请求的变量: {variables}，层级: {levels or '全部'}。\n"
                f"该 .idx 文件实际可用的变量: {available['variables']}\n"
                f"可用的层级示例: {available['level_samples']}"
            )

        dest_path = build_dest_path(
            date, cycle, member, forecast_hour, variables, levels, dest_root, product
        )
        total_bytes = sum(e.byte_count or 0 for e in selected)
        emit_progress(
            f"命中 {len(selected)} 个 GRIB message，准备分块下载 "
            f"{_fmt_size(total_bytes) if total_bytes else '未知大小'}"
        )
        downloaded = await asyncio.to_thread(
            self._download_ranges,
            grib_url,
            selected,
            dest_path,
            total_bytes,
            on_progress,
        )
        emit_progress(
            f"GEFS 文件下载完成：{dest_path} ({_fmt_size(downloaded)})"
        )
        return GEFSDownloadFile(
            member=member,
            forecast_hour=forecast_hour,
            idx_url=idx_url,
            grib_url=grib_url,
            file_path=dest_path,
            selected_entries=selected,
            missing=missing,
            downloaded_bytes=downloaded,
        )

    def build_grib_url(
        self,
        date: str,
        cycle: str,
        forecast_hour: int,
        product: str = DEFAULT_PRODUCT,
        member: str = "c00",
    ) -> str:
        date = normalize_date(date)
        cycle = normalize_cycle(cycle)
        forecast_hour = normalize_forecast_hour(forecast_hour)
        product = normalize_product(product)
        member = normalize_member(member)
        prod_info = _get_product_info(product)
        prefix = _member_prefix(member)
        return (
            f"{self._base_url}/gefs.{date}/{cycle}/atmos/"
            f"{prod_info['dir']}/{prefix}.t{cycle}z."
            f"{prod_info['code']}.{prod_info['resolution']}.f{forecast_hour:03d}"
        )

    @staticmethod
    def _fetch_text(url: str) -> str:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text

    @staticmethod
    def _download_ranges(
        url: str,
        entries: list[GEFSIndexEntry],
        dest: Path,
        total_bytes: int,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        tmp = dest.with_suffix(dest.suffix + ".part")
        if tmp.exists():
            tmp.unlink()
        if dest.exists():
            dest.unlink()

        done = 0
        with httpx.Client(timeout=None, follow_redirects=True) as client:
            with tmp.open("wb") as out:
                for entry in entries:
                    if cancel_requested():
                        raise RuntimeError("下载已取消")
                    headers = {"Range": entry.range_header}
                    with client.stream("GET", url, headers=headers) as resp:
                        if resp.status_code not in (200, 206):
                            resp.raise_for_status()
                        for chunk in resp.iter_bytes(chunk_size=CHUNK_SIZE):
                            if cancel_requested():
                                raise RuntimeError("下载已取消")
                            out.write(chunk)
                            done += len(chunk)
                            if on_progress and total_bytes > 0:
                                on_progress(done, total_bytes)
        tmp.replace(dest)
        return done


def _get_product_info(product: str) -> dict:
    info = _PRODUCT_MAP.get(product)
    if info is None:
        raise ValueError(
            f"不支持的产品: {product}。可用: {', '.join(_PRODUCT_MAP)}"
        )
    return info


def _member_prefix(member: str) -> str:
    if member == "c00":
        return "gec00"
    if member == "avg":
        return "geavg"
    return f"gep{member[1:]}" if member.startswith("p") else f"ge{member}"


def _summarize_available(entries: list[GEFSIndexEntry]) -> dict:
    variables = sorted(set(e.variable for e in entries))
    levels = sorted(set(e.level for e in entries))
    return {
        "variables": ", ".join(variables[:20]) if variables else "(空)",
        "variable_count": len(variables),
        "level_samples": ", ".join(levels[:10]) if levels else "(空)",
        "level_count": len(levels),
    }


def parse_gefs_idx(text: str) -> list[GEFSIndexEntry]:
    """Parse a GEFS .idx file. Same col1:col2:... format as GFS .idx.
    
    GEFS .idx files are already member-specific (one file per member).
    The member column is not needed for filtering — set to empty string.
    """
    entries: list[GEFSIndexEntry] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(":")
        if len(parts) < 6:
            continue
        try:
            index = int(parts[0])
            start = int(parts[1])
        except ValueError:
            continue
        variable = parts[3].strip().upper()
        level = parts[4].strip()
        forecast = parts[5].strip()
        entries.append(
            GEFSIndexEntry(
                index=index,
                start_byte=start,
                end_byte=None,
                member="",
                variable=variable,
                level=level,
                forecast=forecast,
                raw=raw,
            )
        )

    with_ends: list[GEFSIndexEntry] = []
    for i, entry in enumerate(entries):
        next_start = entries[i + 1].start_byte if i + 1 < len(entries) else None
        end = next_start - 1 if next_start is not None else None
        with_ends.append(
            GEFSIndexEntry(
                index=entry.index,
                start_byte=entry.start_byte,
                end_byte=end,
                member=entry.member,
                variable=entry.variable,
                level=entry.level,
                forecast=entry.forecast,
                raw=entry.raw,
            )
        )
    return with_ends


def _parse_member_from_idx(raw: str) -> str:
    m = re.search(r"ge([cp]\d{2})", raw)
    if m:
        return m.group(1)
    if re.search(r"geavg|geavrg|gemean", raw, re.I):
        return "avg"
    if re.search(r"gespr", raw, re.I):
        return "avg"
    return raw


def select_gefs_entries(
    entries: list[GEFSIndexEntry],
    variables: list[str],
    levels: list[str] | None = None,
    member: str = "c00",
) -> tuple[list[GEFSIndexEntry], list[dict]]:
    """Select GEFS index entries by variable and level. 
    
    Note: GEFS .idx files are already member-specific (one file per member).
    The member parameter is accepted for API compatibility but not used for filtering.
    """
    variables = normalize_variables(variables)
    levels = normalize_levels(levels)
    selected: list[GEFSIndexEntry] = []
    missing: list[dict] = []

    for variable in variables:
        candidates = [e for e in entries if e.variable == variable]
        if levels:
            for level in levels:
                matched = [e for e in candidates if normalize_level_text(e.level) == level]
                if matched:
                    selected.extend(matched)
                else:
                    missing.append({"variable": variable, "level": level})
        elif candidates:
            selected.extend(candidates)
        else:
            missing.append({"variable": variable, "level": None})

    return selected, missing


def build_dest_path(
    date: str,
    cycle: str,
    member: str,
    forecast_hour: int,
    variables: list[str],
    levels: list[str] | None,
    dest_dir: Path,
    product: str = DEFAULT_PRODUCT,
) -> Path:
    product = normalize_product(product)
    vars_part = "_".join(normalize_variables(variables))
    if levels:
        level_hash = hashlib.sha1("|".join(normalize_levels(levels)).encode()).hexdigest()[:8]
        vars_part = f"{vars_part}_{level_hash}"
    product_part = product.replace(".", "_")
    return dest_dir / (
        f"gefs_{date}_{cycle}z_{product_part}_{member}_"
        f"f{forecast_hour:03d}_{vars_part}.grib2"
    )


def build_request_id(
    date: str,
    cycle: str,
    member: str,
    forecast_hour: int,
    variables: list[str],
    levels: list[str] | None,
    product: str = DEFAULT_PRODUCT,
) -> str:
    normalized_date = normalize_date(date)
    normalized_cycle = normalize_cycle(cycle)
    normalized_member = normalize_member(member)
    normalized_hour = normalize_forecast_hour(forecast_hour)
    normalized_product = normalize_product(product)
    key = "|".join(
        [
            normalized_date,
            normalized_cycle,
            normalized_member,
            str(normalized_hour),
            normalized_product,
            ",".join(normalize_variables(variables)),
            ",".join(normalize_levels(levels) or []),
        ]
    )
    digest = hashlib.sha1(key.encode()).hexdigest()[:10]
    product_part = normalized_product.replace(".", "-")
    return (
        f"gefs-{normalized_date}-{normalized_cycle}-{product_part}-"
        f"f{normalized_hour:03d}-{normalized_member}-{digest}"
    )


def dataset_id_for_product(product: str = DEFAULT_PRODUCT) -> str:
    return f"gefs-{normalize_product(product).replace('.', '-')}"


def normalize_date(date: str) -> str:
    cleaned = re.sub(r"[^0-9]", "", str(date))
    if not re.fullmatch(r"\d{8}", cleaned):
        raise ValueError("date 必须是 YYYYMMDD 或 YYYY-MM-DD")
    return cleaned


def normalize_cycle(cycle: str) -> str:
    cleaned = str(cycle).strip().lower().replace("z", "")
    if re.fullmatch(r"\d", cleaned):
        cleaned = f"0{cleaned}"
    if cleaned not in VALID_CYCLES:
        raise ValueError("cycle 只支持 00、06、12、18")
    return cleaned


def normalize_forecast_hour(forecast_hour: int) -> int:
    value = int(forecast_hour)
    if value < 0 or value > MAX_FORECAST_HOUR:
        raise ValueError(f"forecast_hour 必须在 0 到 {MAX_FORECAST_HOUR} 之间")
    return value


def normalize_forecast_hours(forecast_hours: list[int]) -> list[int]:
    if not forecast_hours:
        raise ValueError("forecast_hours 不能为空")
    return [normalize_forecast_hour(h) for h in forecast_hours]


def normalize_variables(variables: list[str]) -> list[str]:
    if not variables:
        raise ValueError("variables 不能为空")
    normalized = []
    for variable in variables:
        value = str(variable).strip().upper()
        if not value:
            continue
        normalized.append(value)
    if not normalized:
        raise ValueError("variables 不能为空")
    return normalized


def normalize_product(product: str) -> str:
    value = str(product or DEFAULT_PRODUCT).strip().lower()
    if not value:
        return DEFAULT_PRODUCT
    return value


def normalize_levels(levels: list[str] | None) -> list[str] | None:
    if levels is None:
        return None
    normalized = [normalize_level_text(level) for level in levels if str(level).strip()]
    return normalized or None


def normalize_level_text(level: str) -> str:
    return re.sub(r"\s+", " ", str(level).strip().lower())


def normalize_member(member: str) -> str:
    value = str(member).strip().lower()
    if re.fullmatch(r"[cp]\d{2}", value):
        return value
    if value in ("c00", "c0", "c", "control", "ctr", "ctrl"):
        return "c00"
    if value in ("avg", "avrg", "mean", "ensemble", "ens", "spread", "spr"):
        return "avg"
    m = re.match(r"p(\d{1,2})", value)
    if m:
        return f"p{int(m.group(1)):02d}"
    raise ValueError(f"无法识别成员标识: {member}，支持的格式: c00、p01-p30、avg")


def normalize_members(members: list[str] | None) -> list[str]:
    if members is None:
        return list(DEFAULT_MEMBERS)
    return [normalize_member(m) for m in members]


def _fmt_size(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
