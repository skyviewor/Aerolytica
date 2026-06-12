"""GFS adapter using NOMADS GRIB2 .idx files and HTTP Range requests."""

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
DEFAULT_PRODUCT = "pgrb2.0p25"
DATASET_ID = "gfs-pgrb2-0p25"
NOMADS_GFS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod"
VALID_CYCLES = {"00", "06", "12", "18"}


@dataclass(frozen=True)
class GFSIndexEntry:
    index: int
    start_byte: int
    end_byte: int | None
    run: str
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
class GFSDownloadFile:
    forecast_hour: int
    idx_url: str
    grib_url: str
    file_path: Path
    selected_entries: list[GFSIndexEntry]
    missing: list[dict]
    downloaded_bytes: int
    source: str = "nomads"


class GFSAdapter:
    """Download selected GFS GRIB2 messages from NOMADS official files."""

    def __init__(self, base_url: str = NOMADS_GFS_BASE):
        self._base_url = base_url.rstrip("/")

    async def download(
        self,
        *,
        date: str,
        cycle: str,
        forecast_hours: list[int],
        variables: list[str],
        product: str = DEFAULT_PRODUCT,
        levels: list[str] | None = None,
        dest_dir: Path | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[GFSDownloadFile]:
        date = normalize_date(date)
        cycle = normalize_cycle(cycle)
        forecast_hours = normalize_forecast_hours(forecast_hours)
        variables = normalize_variables(variables)
        product = normalize_product(product)
        levels = normalize_levels(levels)
        dest_root = dest_dir or config_output_dir()
        dest_root.mkdir(parents=True, exist_ok=True)

        results: list[GFSDownloadFile] = []
        for fhour in forecast_hours:
            if cancel_requested():
                raise RuntimeError("下载已取消")
            result = await self.download_one(
                date=date,
                cycle=cycle,
                forecast_hour=fhour,
                variables=variables,
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
        product: str = DEFAULT_PRODUCT,
        levels: list[str] | None = None,
        dest_dir: Path | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> GFSDownloadFile:
        date = normalize_date(date)
        cycle = normalize_cycle(cycle)
        forecast_hour = normalize_forecast_hour(forecast_hour)
        variables = normalize_variables(variables)
        product = normalize_product(product)
        levels = normalize_levels(levels)
        dest_root = dest_dir or config_output_dir()
        dest_root.mkdir(parents=True, exist_ok=True)

        grib_url = self.build_grib_url(date, cycle, forecast_hour, product)
        idx_url = f"{grib_url}.idx"
        emit_progress(
            f"正在读取 GFS 索引：{date} {cycle}Z f{forecast_hour:03d}，变量={', '.join(variables)}"
        )

        idx_text = await asyncio.to_thread(self._fetch_text, idx_url)
        entries = parse_gfs_idx(idx_text)
        selected, missing = select_gfs_entries(entries, variables, levels)
        if not selected:
            raise RuntimeError("GFS .idx 中没有找到匹配字段")

        dest_path = build_dest_path(
            date, cycle, forecast_hour, variables, levels, dest_root, product
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
        emit_progress(f"GFS 文件下载完成：{dest_path} ({_fmt_size(downloaded)})")
        return GFSDownloadFile(
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
    ) -> str:
        date = normalize_date(date)
        cycle = normalize_cycle(cycle)
        forecast_hour = normalize_forecast_hour(forecast_hour)
        product = normalize_product(product)
        return (
            f"{self._base_url}/gfs.{date}/{cycle}/atmos/"
            f"gfs.t{cycle}z.{product}.f{forecast_hour:03d}"
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
        entries: list[GFSIndexEntry],
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


def parse_gfs_idx(text: str) -> list[GFSIndexEntry]:
    entries: list[GFSIndexEntry] = []
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
        run = parts[2]
        variable = parts[3].strip().upper()
        level = parts[4].strip()
        forecast = parts[5].strip()
        entries.append(
            GFSIndexEntry(
                index=index,
                start_byte=start,
                end_byte=None,
                run=run,
                variable=variable,
                level=level,
                forecast=forecast,
                raw=raw,
            )
        )

    with_ends: list[GFSIndexEntry] = []
    for i, entry in enumerate(entries):
        next_start = entries[i + 1].start_byte if i + 1 < len(entries) else None
        end = next_start - 1 if next_start is not None else None
        with_ends.append(
            GFSIndexEntry(
                index=entry.index,
                start_byte=entry.start_byte,
                end_byte=end,
                run=entry.run,
                variable=entry.variable,
                level=entry.level,
                forecast=entry.forecast,
                raw=entry.raw,
            )
        )
    return with_ends


def select_gfs_entries(
    entries: list[GFSIndexEntry],
    variables: list[str],
    levels: list[str] | None = None,
) -> tuple[list[GFSIndexEntry], list[dict]]:
    variables = normalize_variables(variables)
    levels = normalize_levels(levels)
    selected: list[GFSIndexEntry] = []
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


def summarize_gfs_inventory(
    entries: list[GFSIndexEntry],
    variables: list[str] | None = None,
) -> list[dict]:
    wanted = set(normalize_variables(variables)) if variables else None
    grouped: dict[tuple[str, str, str], dict] = {}
    for entry in entries:
        if wanted is not None and entry.variable not in wanted:
            continue
        key = (entry.variable, entry.level, entry.forecast)
        item = grouped.setdefault(
            key,
            {
                "variable": entry.variable,
                "level": entry.level,
                "forecast": entry.forecast,
                "message_count": 0,
                "byte_count": 0,
                "examples": [],
            },
        )
        item["message_count"] += 1
        if entry.byte_count is not None:
            item["byte_count"] += entry.byte_count
        if len(item["examples"]) < 3:
            item["examples"].append(entry.raw)
    return sorted(
        grouped.values(),
        key=lambda item: (str(item["variable"]), str(item["level"]), str(item["forecast"])),
    )


def build_dest_path(
    date: str,
    cycle: str,
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
    return dest_dir / f"gfs_{date}_{cycle}z_{product_part}_f{forecast_hour:03d}_{vars_part}.grib2"


def build_request_id(
    date: str,
    cycle: str,
    forecast_hour: int,
    variables: list[str],
    levels: list[str] | None,
    product: str = DEFAULT_PRODUCT,
) -> str:
    normalized_date = normalize_date(date)
    normalized_cycle = normalize_cycle(cycle)
    normalized_hour = normalize_forecast_hour(forecast_hour)
    normalized_product = normalize_product(product)
    key = "|".join(
        [
            normalized_date,
            normalized_cycle,
            str(normalized_hour),
            normalized_product,
            ",".join(normalize_variables(variables)),
            ",".join(normalize_levels(levels) or []),
        ]
    )
    digest = hashlib.sha1(key.encode()).hexdigest()[:10]
    product_part = normalized_product.replace(".", "-")
    return (
        f"gfs-{normalized_date}-{normalized_cycle}-{product_part}-"
        f"f{normalized_hour:03d}-{digest}"
    )


def dataset_id_for_product(product: str = DEFAULT_PRODUCT) -> str:
    return f"gfs-{normalize_product(product).replace('.', '-')}"


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
    if value < 0 or value > 384:
        raise ValueError("forecast_hour 必须在 0 到 384 之间")
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
    if not re.fullmatch(r"[a-z0-9]+(?:\.[a-z0-9]+)+", value):
        raise ValueError("product 必须类似 pgrb2.0p25、pgrb2b.0p25、pgrb2.0p50")
    return value


def normalize_levels(levels: list[str] | None) -> list[str] | None:
    if levels is None:
        return None
    normalized = [normalize_level_text(level) for level in levels if str(level).strip()]
    return normalized or None


def normalize_level_text(level: str) -> str:
    return re.sub(r"\s+", " ", str(level).strip().lower())


def _fmt_size(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
