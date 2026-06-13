"""CAMS global forecast cycle availability helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

CAMS_FORECAST_CYCLES = (
    {"cycle": "00", "guaranteed_available_hour_utc": 10},
    {"cycle": "12", "guaranteed_available_hour_utc": 22},
)
CAMS_FORECAST_DATASET_ID = "cams-global-atmospheric-composition-forecasts"
CAMS_COSTING_URL = (
    "https://ads.atmosphere.copernicus.eu/api/retrieve/v1/processes/"
    f"{CAMS_FORECAST_DATASET_ID}/costing"
)
PROBE_VARIABLE = "particulate_matter_2.5um"


async def get_latest_available_cams_forecast_cycle(
    reference_time: datetime | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Probe ADS validation and return the latest accepted CAMS forecast cycle."""
    now = _normalize_reference_time(reference_time)
    candidates = _candidate_cycles(now)
    checks: list[dict] = []
    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            transport=transport,
        ) as client:
            for candidate in candidates:
                response = await client.post(
                    CAMS_COSTING_URL,
                    json={"inputs": _probe_request(candidate["date"], candidate["cycle"])},
                )
                response.raise_for_status()
                payload = response.json()
                available = payload.get("request_is_valid") is not False
                checks.append(
                    {
                        **candidate,
                        "available": available,
                        "reason": payload.get("invalid_reason", ""),
                    }
                )
                if available:
                    schedule = latest_guaranteed_cams_forecast_cycle(now)
                    return {
                        **schedule,
                        "latest_available": checks[-1],
                        "availability_basis": "ads_costing_api",
                        "actual_ads_probe_performed": True,
                        "checks": checks,
                    }
    except (httpx.HTTPError, ValueError) as exc:
        schedule = latest_guaranteed_cams_forecast_cycle(now)
        return {
            **schedule,
            "latest_available": schedule["latest_guaranteed"],
            "availability_basis": "official_schedule_fallback",
            "actual_ads_probe_performed": False,
            "probe_error": str(exc),
            "checks": checks,
        }
    schedule = latest_guaranteed_cams_forecast_cycle(now)
    return {
        **schedule,
        "latest_available": schedule["latest_guaranteed"],
        "availability_basis": "official_schedule_fallback",
        "actual_ads_probe_performed": True,
        "probe_error": "ADS 未确认最近三天内任何 CAMS 预报起报时次可用",
        "checks": checks,
    }


def latest_guaranteed_cams_forecast_cycle(reference_time: datetime | None = None) -> dict:
    """Return the latest CAMS cycle guaranteed available from ADS."""
    now = _normalize_reference_time(reference_time)
    candidates = []
    for day_offset in range(0, 3):
        day = (now - timedelta(days=day_offset)).date()
        for item in CAMS_FORECAST_CYCLES:
            cycle = item["cycle"]
            run_time = datetime(
                day.year,
                day.month,
                day.day,
                int(cycle),
                tzinfo=timezone.utc,
            )
            guaranteed_at = datetime(
                day.year,
                day.month,
                day.day,
                item["guaranteed_available_hour_utc"],
                tzinfo=timezone.utc,
            )
            candidates.append(
                {
                    "date": day.isoformat(),
                    "cycle": cycle,
                    "run_time_utc": _iso(run_time),
                    "guaranteed_available_at_utc": _iso(guaranteed_at),
                    "guaranteed_available": now >= guaranteed_at,
                }
            )

    candidates.sort(key=lambda item: item["run_time_utc"], reverse=True)
    guaranteed = next(item for item in candidates if item["guaranteed_available"])
    newer = [
        item
        for item in candidates
        if item["run_time_utc"] > guaranteed["run_time_utc"]
        and item["run_time_utc"] <= _iso(now)
    ]
    return {
        "checked_at_utc": _iso(now),
        "latest_guaranteed": guaranteed,
        "newer_not_yet_guaranteed": newer,
        "schedule": [
            {
                "cycle": item["cycle"],
                "guaranteed_available_by_utc": (
                    f"{item['guaranteed_available_hour_utc']:02d}:00"
                ),
            }
            for item in CAMS_FORECAST_CYCLES
        ],
    }


def _candidate_cycles(now: datetime) -> list[dict[str, str]]:
    candidates = []
    for day_offset in range(0, 3):
        day = (now - timedelta(days=day_offset)).date()
        for item in CAMS_FORECAST_CYCLES:
            cycle = item["cycle"]
            run_time = datetime(day.year, day.month, day.day, int(cycle), tzinfo=timezone.utc)
            if run_time <= now:
                candidates.append(
                    {
                        "date": day.isoformat(),
                        "cycle": cycle,
                        "run_time_utc": _iso(run_time),
                    }
                )
    return sorted(candidates, key=lambda item: item["run_time_utc"], reverse=True)


def _probe_request(day: str, cycle: str) -> dict:
    return {
        "variable": [PROBE_VARIABLE],
        "date": f"{day}/{day}",
        "time": [f"{cycle}:00"],
        "leadtime_hour": ["0"],
        "type": ["forecast"],
        "data_format": "grib",
    }


def _normalize_reference_time(reference_time: datetime | None) -> datetime:
    value = reference_time or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
