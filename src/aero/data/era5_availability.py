"""ERA5 CDS availability resolution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import structlog

from aero.core.debug_log import debug_exception, debug_log
from aero.data.era5_variable_map import resolve_cds_name

logger = structlog.get_logger()

VALID_SOURCES = {"cds"}
CHECK_TIMEOUT = 15  # seconds


@dataclass(frozen=True)
class ERA5SourceStatus:
    source: str
    available: bool
    reason: str
    latest_date: str | None = None
    checked_at: str | None = None


@dataclass(frozen=True)
class ERA5AvailabilityDecision:
    requested_source: str
    selected_source: str | None
    available: bool
    cds: ERA5SourceStatus
    reason: str


def normalize_source(source: str | None) -> str:
    value = str(source or "cds").strip().lower()
    if value not in VALID_SOURCES:
        raise ValueError(f"source 只支持: {', '.join(sorted(VALID_SOURCES))}")
    return value


async def resolve_era5_source(
    variables: list[str],
    year: int,
    month: int,
    day: int | None = None,
    pressure_level: int | None = None,
    source: str = "cds",
) -> ERA5AvailabilityDecision:
    requested = normalize_source(source)
    canonical_vars = [resolve_cds_name(v) for v in variables]

    now = _now()
    debug_log("era5.availability.check_start", requested_source=requested,
              variables=canonical_vars, year=year, month=f"{month:02d}", day=day,
              pressure_level=pressure_level)

    cds_status = await _check_cds(canonical_vars, year, month, day, pressure_level, now)

    if cds_status.available:
        return ERA5AvailabilityDecision(
            requested_source=requested,
            selected_source="cds",
            available=True,
            cds=cds_status,
            reason=cds_status.reason,
        )

    debug_log("era5.availability.cds_unavailable",
              reason=cds_status.reason)
    return ERA5AvailabilityDecision(
        requested_source=requested,
        selected_source=None,
        available=False,
        cds=cds_status,
        reason=cds_status.reason,
    )


async def check_era5_source_availability(
    variables: list[str],
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
    pressure_level: int | None = None,
    source: str = "cds",
    refresh: bool = False,
) -> dict:
    requested = normalize_source(source)
    canonical_vars = [resolve_cds_name(v) for v in variables]
    now = _now()

    result: dict = {
        "status": "success",
        "requested_source": requested,
        "variables": canonical_vars,
        "year": year,
        "month": month,
        "day": day,
        "pressure_level": pressure_level,
        "sources": {},
        "references": [
            "https://cds.climate.copernicus.eu/datasets",
        ],
    }

    if year is not None and month is not None:
        result["sources"]["cds"] = asdict(
            await _check_cds(canonical_vars, year, month, day, pressure_level, now)
        )
    else:
        result["sources"]["cds"] = {
            "source": "cds",
            "available": True,
            "reason": "CDS 官方源通常按请求动态生成 ERA5；实际可用性取决于 CDS 凭证、数据集更新和队列状态。",
            "time_range": "ERA5 常规数据通常从 1940 年起，近实时 ERA5T 约有数天延迟。",
            "checked_at": _iso(now),
        }

    return result


async def _check_cds(
    canonical_vars: list[str],
    year: int,
    month: int,
    day: int | None,
    pressure_level: int | None,
    now: datetime,
) -> ERA5SourceStatus:
    try:
        import cdsapi  # noqa: F401
    except ImportError:
        return ERA5SourceStatus(
            source="cds",
            available=False,
            reason="CDS 需要 cdsapi 库但未安装。请 pip install cdsapi。",
            checked_at=_iso(now),
        )

    try:
        from aero.core.config import AeroConfig

        config_path: Path | None = None
        cwd = Path.cwd()
        for parent in [cwd, *cwd.parents]:
            candidate = parent / "aero.yaml"
            if candidate.exists():
                config_path = candidate
                break

        if config_path is None:
            return ERA5SourceStatus(
                source="cds",
                available=False,
                reason="未找到 aero.yaml 项目配置文件。",
                checked_at=_iso(now),
            )

        config = AeroConfig.load(config_path)
        cds_cfg = config.credentials.cds
        if not cds_cfg.key:
            return ERA5SourceStatus(
                source="cds",
                available=False,
                reason="CDS API key 未设置。请用 /configure cds 配置。",
                checked_at=_iso(now),
            )

        dataset_id = (
            "reanalysis-era5-pressure-levels" if pressure_level
            else "reanalysis-era5-single-levels"
        )
        return ERA5SourceStatus(
            source="cds",
            available=True,
            reason=f"CDS 已配置，目标数据集: {dataset_id}",
            latest_date=None,
            checked_at=_iso(now),
        )
    except Exception as e:
        debug_exception("era5.availability.cds_check_failed", e,
                        config_path=str(config_path) if 'config_path' in locals() else "unknown")
        return ERA5SourceStatus(
            source="cds",
            available=False,
            reason=f"CDS 配置检查失败: {e}",
            checked_at=_iso(now),
        )


def _not_checked(source: str, now: datetime) -> ERA5SourceStatus:
    return ERA5SourceStatus(
        source=source,
        available=False,
        reason="未检查",
        checked_at=_iso(now),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
