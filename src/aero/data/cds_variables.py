"""CDS variable lookup — fetches from CDS catalogue API, caches locally."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import structlog

logger = structlog.get_logger()

_HERE = Path(__file__).parent
_REFERENCE_FILE = _HERE / "cds_reference.json"

CACHE_DIR = Path.home() / ".cache" / "aero"
CACHE_FILE = CACHE_DIR / "cds_variables.json"
CACHE_TTL = timedelta(hours=24)

DATASETS: dict[str, dict] = {
    "reanalysis-era5-pressure-levels": {
        "level_type": "pressure",
        "dataset_label": "高空（气压层）",
    },
    "reanalysis-era5-single-levels": {
        "level_type": "surface",
        "dataset_label": "地表（单层）",
    },
    "reanalysis-era5-land": {
        "level_type": "surface",
        "dataset_label": "地表（陆面）",
    },
    "reanalysis-era5-pressure-levels-monthly-means": {
        "level_type": "pressure",
        "dataset_label": "高空月均值",
        "is_monthly": True,
    },
    "reanalysis-era5-single-levels-monthly-means": {
        "level_type": "surface",
        "dataset_label": "地表月均值",
        "is_monthly": True,
    },
    "reanalysis-era5-land-monthly-means": {
        "level_type": "surface",
        "dataset_label": "陆面月均值",
        "is_monthly": True,
    },
}


def _load_reference() -> dict:
    with open(_REFERENCE_FILE) as f:
        return json.load(f)


async def _get_form_url(dataset_id: str) -> str | None:
    url = f"https://cds.climate.copernicus.eu/api/catalogue/v1/collections/{dataset_id}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        for link in data.get("links", []):
            if link.get("rel") == "form":
                return link["href"]
    return None


def _extract_variables_from_form(form: list[dict]) -> list[dict]:
    for param in form:
        if param.get("name") != "variable":
            continue
        details = param.get("details", {})

        # Flat format: details.values + details.labels
        values = details.get("values", [])
        labels = details.get("labels", {})
        if values:
            return [{"name": v, "label": labels.get(v, v)} for v in values]

        # Nested format: details.groups[].values + labels
        groups = details.get("groups", [])
        if groups:
            all_vars = []
            seen = set()
            for group in groups:
                for v in group.get("values", []):
                    if v in seen:
                        continue
                    seen.add(v)
                    all_vars.append({"name": v, "label": group.get("labels", {}).get(v, v)})
            return all_vars
    return []


async def _fetch_one_dataset(dataset_id: str, level_type: str, dataset_label: str) -> list[dict]:
    form_url = await _get_form_url(dataset_id)
    if not form_url:
        logger.warning("no form url found", dataset=dataset_id)
        return []
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(form_url)
        resp.raise_for_status()
        form = resp.json()
    chinese = _load_reference()["chinese_labels"]
    variables = _extract_variables_from_form(form)
    return [{
        "name": v["name"],
        "label": chinese.get(v["name"], v["label"]),
        "level_type": level_type,
        "dataset": dataset_id,
        "dataset_label": dataset_label,
    } for v in variables]


async def fetch_cds_variables(dataset_ids: list[str] | None = None) -> list[dict]:
    all_vars = []
    seen = set()
    ids = dataset_ids or list(DATASETS.keys())
    for dataset_id in ids:
        info = DATASETS.get(dataset_id)
        if info is None:
            logger.warning("unknown dataset", dataset=dataset_id)
            continue
        try:
            vars_ = await _fetch_one_dataset(dataset_id, info["level_type"], info["dataset_label"])
            new_count = 0
            for v in vars_:
                if v["name"] not in seen:
                    seen.add(v["name"])
                    all_vars.append(v)
                    new_count += 1
            logger.info("fetched", dataset=dataset_id, count=len(vars_), new=new_count)
        except Exception as e:
            logger.warning("failed to fetch", dataset=dataset_id, error=str(e))
    return all_vars


def _load_cache() -> list[dict] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE) as f:
            cached = json.load(f)
        cached_at = datetime.fromisoformat(cached["cached_at"])
        if datetime.now() - cached_at > CACHE_TTL:
            return None
        return cached["variables"]
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def _save_cache(variables: list[dict]):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump({
            "cached_at": datetime.now().isoformat(),
            "count": len(variables),
            "variables": variables,
        }, f, ensure_ascii=False, indent=2)


def _fallback_variables() -> list[dict]:
    ref = _load_reference()
    fallback = ref.get("fallback_variables", [])
    return [
        {"name": v["name"], "label": v["label"],
         "level_type": v["level_type"],
         "dataset": "", "dataset_label": ""}
        for v in fallback
    ]


async def get_cds_variables(dataset_ids: list[str] | None = None) -> list[dict]:
    cached = _load_cache()
    if cached is not None:
        if dataset_ids:
            return [v for v in cached if v.get("dataset") in dataset_ids]
        return cached

    live_vars = []
    fetch_ok = False
    try:
        live_vars = await fetch_cds_variables(dataset_ids)
        if live_vars:
            fetch_ok = True
    except Exception as e:
        logger.warning("cds catalogue fetch failed", error=str(e))

    fallback_vars = _fallback_variables()

    if fetch_ok and live_vars:
        live_levels = {v["level_type"] for v in live_vars}
        seen = {v["name"] for v in live_vars}
        supplemented = []
        for fv in fallback_vars:
            if fv["level_type"] not in live_levels and fv["name"] not in seen:
                seen.add(fv["name"])
                supplemented.append(fv)
        all_vars = live_vars + supplemented
        if supplemented:
            logger.info("supplemented from fallback", count=len(supplemented),
                        missing_levels=[lt for lt in {"pressure", "surface"} if lt not in live_levels])
        _save_cache(all_vars)
        if dataset_ids:
            return [v for v in all_vars if v.get("dataset") in dataset_ids]
        return all_vars

    logger.warning("using fallback variable list", count=len(fallback_vars))
    return fallback_vars


def search_cds_variables(variables: list[dict], keyword: str) -> list[dict]:
    ref = _load_reference()
    aliases = ref.get("keyword_aliases", {})
    kw = keyword.strip()

    name_set: set[str] = set()
    for alias_name, var_list in aliases.items():
        if kw in alias_name or alias_name in kw:
            name_set.update(var_list)
    if kw in aliases:
        name_set.update(aliases[kw])
    name_set.update(aliases.get(kw.lower(), []))

    results = []
    kw_lower = kw.lower()
    for v in variables:
        name = v["name"]
        label = v["label"]
        if (kw_lower in name or name in kw_lower
                or kw in label or label in kw
                or name in name_set):
            results.append(v)

    return results


def describe_cds_dataset(dataset_id: str | None = None) -> dict:
    ref = _load_reference()
    datasets = ref.get("datasets", {})
    comparisons = ref.get("dataset_comparisons", {})

    if dataset_id:
        info = datasets.get(dataset_id)
        if not info:
            return {
                "found": False,
                "dataset_id": dataset_id,
                "message": f"未知数据集: {dataset_id}。可选: {list(datasets.keys())}",
            }
        related = {}
        for key, comp in comparisons.items():
            if dataset_id in key or (isinstance(comp, dict) and
                any(dataset_id in str(v) for v in comp.values() if isinstance(v, str))):
                related[key] = comp
        return {
            "found": True,
            "dataset": info,
            "comparisons": related if related else None,
            "references": [
                "https://cds.climate.copernicus.eu/datasets",
                f"https://cds.climate.copernicus.eu/datasets/{dataset_id}",
            ],
        }

    summary = []
    for did, info in datasets.items():
        summary.append({
            "id": did,
            "label": info["label"],
            "level_type": info.get("level_type", ""),
            "resolution": info.get("resolution", ""),
            "temporal": info.get("temporal", ""),
            "variables_count": info.get("variables_count", ""),
            "brief": info["description"].split("。")[0] + "。",
        })

    return {
        "found": True,
        "datasets": summary,
        "comparisons": {
            "single_levels_vs_land": comparisons.get("single_levels_vs_land", {}),
        },
        "note": "如需查看某个数据集的详细信息（完整描述、使用场景、注意事项），请指定 dataset_id 再次查询。",
        "references": [
            "https://cds.climate.copernicus.eu/datasets",
        ],
    }
