"""CAMS ADS variable lookup and alias resolution."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

CACHE_DIR = Path.home() / ".cache" / "aero"
CACHE_FILE = CACHE_DIR / "cams_variables.json"
CACHE_TTL = timedelta(hours=24)

EAC4_DATASET_ID = "cams-global-reanalysis-eac4"
FORECAST_DATASET_ID = "cams-global-atmospheric-composition-forecasts"
DATASETS = {EAC4_DATASET_ID, FORECAST_DATASET_ID}

ADS_CATALOGUE_URL = "https://ads.atmosphere.copernicus.eu/api/catalogue/v1/collections"

COMMON_ALIASES: dict[str, str] = {
    "aod": "total_aerosol_optical_depth_550nm",
    "aod550": "total_aerosol_optical_depth_550nm",
    "bc_aod": "black_carbon_aerosol_optical_depth_550nm",
    "black_carbon_aod": "black_carbon_aerosol_optical_depth_550nm",
    "co": "carbon_monoxide",
    "gtco3": "total_column_ozone",
    "no2": "nitrogen_dioxide",
    "o3": "ozone",
    "pm1": "particulate_matter_1um",
    "pm10": "particulate_matter_10um",
    "pm2.5": "particulate_matter_2.5um",
    "pm2_5": "particulate_matter_2.5um",
    "pm25": "particulate_matter_2.5um",
    "particulate_matter_2_5um": "particulate_matter_2.5um",
    "particulate_matter_2_5_um": "particulate_matter_2.5um",
    "t2m": "2m_temperature",
    "tcco": "total_column_carbon_monoxide",
    "tcno2": "total_column_nitrogen_dioxide",
    "tco3": "total_column_ozone",
    "tcwv": "total_column_water_vapour",
    "total_ozone": "total_column_ozone",
    "u10": "10m_u_component_of_wind",
    "v10": "10m_v_component_of_wind",
}

CHINESE_ALIASES: dict[str, tuple[str, ...]] = {
    "ozone": ("臭氧", "臭氧浓度", "高空臭氧"),
    "total_column_ozone": ("臭氧柱总量", "总柱臭氧", "臭氧总量"),
    "carbon_monoxide": ("一氧化碳", "高空一氧化碳"),
    "total_column_carbon_monoxide": ("一氧化碳柱总量", "总柱一氧化碳"),
    "nitrogen_dioxide": ("二氧化氮", "高空二氧化氮"),
    "total_column_nitrogen_dioxide": ("二氧化氮柱总量", "总柱二氧化氮"),
    "particulate_matter_2.5um": ("pm2.5", "pm25", "细颗粒物"),
    "particulate_matter_10um": ("pm10", "可吸入颗粒物"),
    "total_aerosol_optical_depth_550nm": ("气溶胶光学厚度", "aod"),
}

FALLBACK_VARIABLES: dict[str, list[dict[str, str]]] = {
    EAC4_DATASET_ID: [
        {"name": "2m_temperature", "label": "2 metre temperature", "level_type": "single"},
        {
            "name": "10m_u_component_of_wind",
            "label": "10 metre U wind component",
            "level_type": "single",
        },
        {
            "name": "10m_v_component_of_wind",
            "label": "10 metre V wind component",
            "level_type": "single",
        },
        {
            "name": "total_column_ozone",
            "label": "Total column ozone",
            "level_type": "single",
        },
        {"name": "ozone", "label": "Ozone", "level_type": "multi"},
        {
            "name": "particulate_matter_2.5um",
            "label": "Particulate matter d < 2.5 um (PM2.5)",
            "level_type": "single",
        },
        {
            "name": "particulate_matter_10um",
            "label": "Particulate matter d < 10 um (PM10)",
            "level_type": "single",
        },
        {
            "name": "total_aerosol_optical_depth_550nm",
            "label": "Total aerosol optical depth at 550 nm",
            "level_type": "single",
        },
        {
            "name": "black_carbon_aerosol_optical_depth_550nm",
            "label": "Black carbon aerosol optical depth at 550 nm",
            "level_type": "single",
        },
        {
            "name": "dust_aerosol_optical_depth_550nm",
            "label": "Dust aerosol optical depth at 550 nm",
            "level_type": "single",
        },
        {
            "name": "sea_salt_aerosol_optical_depth_550nm",
            "label": "Sea salt aerosol optical depth at 550 nm",
            "level_type": "single",
        },
        {
            "name": "sulphate_aerosol_optical_depth_550nm",
            "label": "Sulphate aerosol optical depth at 550 nm",
            "level_type": "single",
        },
        {
            "name": "carbon_monoxide",
            "label": "Carbon monoxide",
            "level_type": "multi",
        },
        {
            "name": "total_column_carbon_monoxide",
            "label": "Total column carbon monoxide",
            "level_type": "single",
        },
        {
            "name": "nitrogen_dioxide",
            "label": "Nitrogen dioxide",
            "level_type": "multi",
        },
        {
            "name": "total_column_nitrogen_dioxide",
            "label": "Total column nitrogen dioxide",
            "level_type": "single",
        },
        {
            "name": "dust_aerosol_0.03-0.55um_mixing_ratio",
            "label": "Dust aerosol (0.03 - 0.55 µm) mixing ratio",
            "level_type": "multi",
        },
        {
            "name": "dust_aerosol_0.55-0.9um_mixing_ratio",
            "label": "Dust aerosol (0.55 - 0.9 µm) mixing ratio",
            "level_type": "multi",
        },
        {
            "name": "dust_aerosol_0.9-20um_mixing_ratio",
            "label": "Dust aerosol (0.9 - 20 µm) mixing ratio",
            "level_type": "multi",
        },
        {
            "name": "sulphate_aerosol_mixing_ratio",
            "label": "Sulphate aerosol mixing ratio",
            "level_type": "multi",
        },
    ],
    FORECAST_DATASET_ID: [],
}
FALLBACK_VARIABLES[FORECAST_DATASET_ID] = [
    dict(item) for item in FALLBACK_VARIABLES[EAC4_DATASET_ID]
]
FALLBACK_VARIABLES[FORECAST_DATASET_ID].extend(
    [
        {
            "name": "ammonium_aerosol_optical_depth_550nm",
            "label": "Ammonium aerosol optical depth at 550 nm",
            "level_type": "single",
        },
        {
            "name": "nitrate_aerosol_optical_depth_550nm",
            "label": "Nitrate aerosol optical depth at 550 nm",
            "level_type": "single",
        },
        {
            "name": "organic_matter_aerosol_optical_depth_550nm",
            "label": "Organic matter aerosol optical depth at 550 nm",
            "level_type": "single",
        },
        {
            "name": "secondary_organic_aerosol_optical_depth_550nm",
            "label": "Secondary organic aerosol optical depth at 550 nm",
            "level_type": "single",
        },
        {
            "name": "ammonium_aerosol_mass_mixing_ratio",
            "label": "Ammonium aerosol mass mixing ratio",
            "level_type": "multi",
        },
        {
            "name": "nitrate_fine_mode_aerosol_mass_mixing_ratio",
            "label": "Nitrate fine mode aerosol mass mixing ratio",
            "level_type": "multi",
        },
        {
            "name": "nitrate_coarse_mode_aerosol_mass_mixing_ratio",
            "label": "Nitrate coarse mode aerosol mass mixing ratio",
            "level_type": "multi",
        },
    ]
)


async def fetch_cams_variables(dataset_id: str) -> list[dict[str, str]]:
    """Fetch CAMS ADS variables from the dataset form JSON."""
    form_url = await _get_form_url(dataset_id)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(form_url)
        response.raise_for_status()
        form = response.json()
    return _extract_variables_from_form(dataset_id, form)


async def get_cams_variables(dataset_id: str | None = None) -> list[dict[str, str]]:
    """Return CAMS variables, using a short-lived cache and offline fallback."""
    dataset_ids = [dataset_id] if dataset_id else sorted(DATASETS)
    cached = _load_cache()
    if cached is not None:
        cached_dataset_ids = {item.get("dataset_id") for item in cached}
        missing_dataset_ids = [ds_id for ds_id in dataset_ids if ds_id not in cached_dataset_ids]
        if not missing_dataset_ids:
            return _filter_dataset(cached, dataset_id)
    else:
        cached = []
        missing_dataset_ids = dataset_ids

    variables: list[dict[str, str]] = []
    try:
        for ds_id in missing_dataset_ids:
            variables.extend(await fetch_cams_variables(ds_id))
    except Exception as exc:
        logger.warning("cams.variable_fetch_failed", error=str(exc))
        variables = _fallback_variables(missing_dataset_ids)
    fetched_dataset_ids = {item.get("dataset_id") for item in variables}
    empty_dataset_ids = [ds_id for ds_id in missing_dataset_ids if ds_id not in fetched_dataset_ids]
    if empty_dataset_ids:
        variables = _merge_variables(variables, _fallback_variables(empty_dataset_ids))
    variables = _merge_variables(cached, variables)
    if variables:
        _save_cache(variables)
    return _filter_dataset(variables, dataset_id)


def search_cams_variables(
    variables: list[dict[str, str]],
    query: str = "",
    level_type: str = "",
) -> list[dict[str, str]]:
    """Search CAMS ADS variables by ADS name, label, alias, or Chinese keyword."""
    query_key = _normalise_key(query)
    wanted_name = COMMON_ALIASES.get(query_key, query_key)
    results: list[dict[str, str]] = []
    for item in variables:
        if level_type and item.get("level_type") != level_type:
            continue
        if not query:
            results.append(item)
            continue
        haystack = _variable_haystack(item)
        if (
            query_key in haystack
            or wanted_name == item["name"]
            or query in CHINESE_ALIASES.get(item["name"], ())
            or any(alias in query for alias in CHINESE_ALIASES.get(item["name"], ()))
        ):
            results.append(item)
    return results


def resolve_cams_variable_names(
    requested: list[str],
    variables: list[dict[str, str]],
) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """Resolve user-facing CAMS aliases to ADS request variable names."""
    by_name = {item["name"]: item for item in variables}
    by_normalised_name = {_normalise_key(item["name"]): item for item in variables}
    resolved: list[str] = []
    warnings: list[str] = []
    records: list[dict[str, str]] = []
    for variable in requested:
        key = _normalise_key(variable)
        name = COMMON_ALIASES.get(key, variable.strip())
        if name not in by_name and key in by_name:
            name = key
        if name in by_name:
            resolved.append(name)
            records.append(by_name[name])
            if name != variable:
                warnings.append(f"{variable} 已解析为 ADS 变量名 {name}")
            continue
        if key in by_normalised_name:
            match = by_normalised_name[key]
            resolved.append(match["name"])
            records.append(match)
            if match["name"] != variable:
                warnings.append(f"{variable} 已解析为 ADS 变量名 {match['name']}")
            continue

        matches = search_cams_variables(variables, variable)
        if len(matches) == 1:
            match = matches[0]
            resolved.append(match["name"])
            records.append(match)
            warnings.append(f"{variable} 已解析为 ADS 变量名 {match['name']}")
        else:
            resolved.append(variable)
            if matches:
                options = ", ".join(item["name"] for item in matches[:6])
                warnings.append(f"{variable} 有多个 CAMS 候选变量：{options}")
            else:
                warnings.append(f"{variable} 未在 CAMS ADS 变量表中找到精确匹配")
    return resolved, warnings, records


def _extract_variables_from_form(
    dataset_id: str,
    form: list[dict[str, Any]],
) -> list[dict[str, str]]:
    variable_field = next((item for item in form if item.get("name") == "variable"), None)
    if not variable_field:
        return []
    details = variable_field.get("details", {})
    groups = details.get("groups", [])
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        group_label = str(group.get("label", ""))
        level_type = "multi" if "multi" in group_label.casefold() else "single"
        labels = group.get("labels", {})
        for value in group.get("values", []):
            if value in seen:
                continue
            seen.add(value)
            results.append(
                {
                    "dataset_id": dataset_id,
                    "name": value,
                    "label": labels.get(value, value),
                    "level_type": level_type,
                    "group": group_label,
                }
            )
    return results


async def _get_form_url(dataset_id: str) -> str:
    url = f"{ADS_CATALOGUE_URL}/{dataset_id}"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
    for link in data.get("links", []):
        if link.get("rel") == "form" and link.get("href"):
            return link["href"]
    raise RuntimeError(f"CAMS ADS catalogue did not expose a form URL for {dataset_id}")


def _fallback_variables(dataset_ids: list[str]) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for dataset_id in dataset_ids:
        for item in FALLBACK_VARIABLES.get(dataset_id, []):
            record = dict(item)
            record["dataset_id"] = dataset_id
            record.setdefault(
                "group",
                "Single level" if item["level_type"] == "single" else "Multi level",
            )
            results.append(record)
    return results


def _filter_dataset(
    variables: list[dict[str, str]],
    dataset_id: str | None,
) -> list[dict[str, str]]:
    if dataset_id is None:
        return variables
    return [item for item in variables if item.get("dataset_id") == dataset_id]


def _merge_variables(
    existing: list[dict[str, str]],
    incoming: list[dict[str, str]],
) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in [*existing, *incoming]:
        dataset_id = item.get("dataset_id", "")
        name = item.get("name", "")
        key = (dataset_id, name)
        if not dataset_id or not name or key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _load_cache() -> list[dict[str, str]] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        cached = json.loads(CACHE_FILE.read_text())
        cached_at = datetime.fromisoformat(cached["cached_at"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
    if datetime.now() - cached_at > CACHE_TTL:
        return None
    variables = cached.get("variables", [])
    return variables if isinstance(variables, list) else None


def _save_cache(variables: list[dict[str, str]]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(
            {
                "cached_at": datetime.now().isoformat(),
                "count": len(variables),
                "variables": variables,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _normalise_key(value: str) -> str:
    return value.strip().casefold().replace(" ", "_").replace("-", "_")


def _variable_haystack(item: dict[str, str]) -> str:
    aliases = " ".join(CHINESE_ALIASES.get(item["name"], ()))
    return " ".join(
        [
            item.get("name", ""),
            item.get("label", ""),
            item.get("level_type", ""),
            item.get("group", ""),
            aliases,
        ]
    ).casefold()
