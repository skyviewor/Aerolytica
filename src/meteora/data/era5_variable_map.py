"""ERA5 variable name mapping — CDS canonical names.

Accepts CDS short names as the canonical input and maps to full CDS variable names.
The canonical CDS name is the authoritative key; add new variables here as needed.
"""

from __future__ import annotations

from typing import Any

# ── surface / single-level variables ────────────────────────────────────

ERA5_SURFACE_VARS: list[dict[str, Any]] = [
    {"cds": "2m_temperature"},
    {"cds": "2m_dewpoint_temperature"},
    {"cds": "10m_u_component_of_wind"},
    {"cds": "10m_v_component_of_wind"},
    {"cds": "mean_sea_level_pressure"},
    {"cds": "surface_pressure"},
    {"cds": "total_precipitation"},
    {"cds": "total_cloud_cover"},
    {"cds": "sea_surface_temperature"},
    {"cds": "skin_temperature"},
    {"cds": "boundary_layer_height"},
    {"cds": "convective_available_potential_energy"},
    {"cds": "snow_depth"},
    {"cds": "lake_ice_depth"},
    {"cds": "lake_ice_temperature"},
    {"cds": "surface_latent_heat_flux"},
    {"cds": "surface_sensible_heat_flux"},
    {"cds": "surface_net_solar_radiation"},
    {"cds": "surface_net_thermal_radiation"},
    {"cds": "top_net_solar_radiation"},
    {"cds": "evaporation"},
    {"cds": "runoff"},
    {"cds": "sub_surface_runoff"},
    {"cds": "total_column_water"},
    {"cds": "total_column_water_vapour"},
    {"cds": "total_column_cloud_liquid_water"},
    {"cds": "total_column_cloud_ice_water"},
    {"cds": "low_cloud_cover"},
    {"cds": "medium_cloud_cover"},
    {"cds": "high_cloud_cover"},
    {"cds": "land_sea_mask"},
]

# ── pressure-level variables ────────────────────────────────────────────

ERA5_PRESSURE_VARS: list[dict[str, Any]] = [
    {"cds": "temperature"},
    {"cds": "u_component_of_wind"},
    {"cds": "v_component_of_wind"},
    {"cds": "geopotential"},
    {"cds": "specific_humidity"},
    {"cds": "relative_humidity"},
    {"cds": "vertical_velocity"},
    {"cds": "divergence"},
    {"cds": "vorticity"},
    {"cds": "potential_vorticity"},
    {"cds": "ozone_mass_mixing_ratio"},
]

# ── short name aliases (user-friendly CDS short names) ──────────────────

CDS_SHORT_NAMES: dict[str, str] = {
    "t2m": "2m_temperature",
    "d2m": "2m_dewpoint_temperature",
    "u10": "10m_u_component_of_wind",
    "v10": "10m_v_component_of_wind",
    "msl": "mean_sea_level_pressure",
    "sp": "surface_pressure",
    "tp": "total_precipitation",
    "tcc": "total_cloud_cover",
    "t": "temperature",
    "u": "u_component_of_wind",
    "v": "v_component_of_wind",
    "z": "geopotential",
    "q": "specific_humidity",
    "r": "relative_humidity",
    "w": "vertical_velocity",
    "d": "divergence",
    "vo": "vorticity",
    "pv": "potential_vorticity",
    "o3": "ozone_mass_mixing_ratio",
    "sst": "sea_surface_temperature",
    "skt": "skin_temperature",
    "blh": "boundary_layer_height",
    "cape": "convective_available_potential_energy",
    "sd": "snow_depth",
    "tcw": "total_column_water",
    "tcwv": "total_column_water_vapour",
    "lcc": "low_cloud_cover",
    "mcc": "medium_cloud_cover",
    "hcc": "high_cloud_cover",
    "lsm": "land_sea_mask",
    "ishf": "surface_latent_heat_flux",
    "inss": "surface_sensible_heat_flux",
    "ie": "evaporation",
    "fsr": "runoff",
    "fal": "sub_surface_runoff",
}

_SURFACE_MAP: dict[str, dict] | None = None
_PRESSURE_MAP: dict[str, dict] | None = None


def _build_maps() -> None:
    global _SURFACE_MAP, _PRESSURE_MAP
    if _SURFACE_MAP is not None:
        return
    _SURFACE_MAP = {v["cds"]: v for v in ERA5_SURFACE_VARS}
    _PRESSURE_MAP = {v["cds"]: v for v in ERA5_PRESSURE_VARS}


def resolve_cds_name(variable: str) -> str:
    """Resolve a CDS short name (t2m) to the canonical CDS variable name."""
    key = variable.strip()
    if key in CDS_SHORT_NAMES:
        return CDS_SHORT_NAMES[key]
    return key


def get_surface_var(cds_name: str) -> dict | None:
    """Return the variable record for a canonical CDS surface variable name."""
    _build_maps()
    assert _SURFACE_MAP is not None
    return _SURFACE_MAP.get(cds_name)


def get_pressure_var(cds_name: str) -> dict | None:
    """Return the variable record for a canonical CDS pressure-level variable name."""
    _build_maps()
    assert _PRESSURE_MAP is not None
    return _PRESSURE_MAP.get(cds_name)
