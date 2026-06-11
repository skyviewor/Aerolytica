"""CDS, ECMWF, and GFS parameter lookup tools."""

from __future__ import annotations

from meteora.toolbox.registry import register_tool


# 数据集类型别名 → 内部 level_type
_DATA_TYPE_ALIASES = {
    "pressure": "pressure",
    "高空": "pressure",
    "气压层": "pressure",
    "上层": "pressure",
    "surface": "surface",
    "地表": "surface",
    "地面": "surface",
    "单层": "surface",
}


@register_tool(
    name="search_cds_variables",
    description=(
        "搜索 CDS 数据集的变量，支持中文/英文关键词和数据集类型别名。\n\n"
        "两种用法：\n"
        "1. 按数据集类型查：「高空」「气压层」（pressure）或「地面」「地表」「单层」（surface）"
        "→ 填 data_type 即可，不填 keyword\n"
        '2. 按关键词查具体要素：keyword="云量"、keyword="温度"、keyword="wind" '
        "→ 所有数据集里搜\n"
        '两者可以组合：keyword="风" data_type="pressure" → 只搜气压层的风场变量'
    ),
    parameters={
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": (
                    "变量名称或关键词，如 云量、降水、温度、风、wind 等。"
                    "不填则返回数据集类型下的全部变量"
                ),
            },
            "data_type": {
                "type": "string",
                "description": (
                    "数据集类型别名：pressure（高空/气压层）或 "
                    "surface（地面/地表/单层）。keyword 和 data_type 至少填一个"
                ),
            },
        },
    },
)
async def search_cds_variables(keyword: str | None = None, data_type: str | None = None) -> dict:
    """Search CDS variables — fetches live list from CDS catalogue (public)."""
    from meteora.data.cds_variables import get_cds_variables
    from meteora.data.cds_variables import search_cds_variables as _search

    level_type = _DATA_TYPE_ALIASES.get(data_type) if data_type else None
    if not keyword and not level_type:
        return {"error": "keyword 和 data_type 至少填一个"}

    variables = await get_cds_variables()
    if keyword:
        results = _search(variables, keyword)
    else:
        results = list(variables)
    if level_type:
        results = [v for v in results if v["level_type"] == level_type]

    # 按 level_type 分类统计
    all_pressure = [v for v in variables if v["level_type"] == "pressure"]
    all_surface = [v for v in variables if v["level_type"] == "surface"]

    if not results:
        msg = "未找到匹配的变量"
        if keyword:
            msg += f"（keyword=「{keyword}」"
        if data_type:
            msg += f"{'，' if keyword else '（'}data_type=「{data_type}」"
        msg += "）"
        hint = ""
        if level_type == "pressure":
            hint = (
                f"。当前数据库中共有 {len(all_pressure)} 个高空变量和 {len(all_surface)} 个地表变量"
            )
            if not keyword and len(all_pressure) == 0:
                hint = (
                    "。CDS 气压层数据拉取失败，正使用离线参考数据（可能不完整）。"
                    "请尝试 keyword 直接搜索（如 keyword=「风」）而不限于高空"
                )
        if not hint:
            hint = f"。当前共有 {len(all_pressure)} 个高空变量和 {len(all_surface)} 个地表变量"
        return {
            "found": False,
            "keyword": keyword or "",
            "data_type": data_type or "",
            "message": msg + hint,
            "total_available": {"pressure": len(all_pressure), "surface": len(all_surface)},
            "suggestions": [
                "总降水",
                "云量",
                "气温",
                "风",
                "位势高度",
                "湿度",
                "气压",
                "海温",
                "辐射",
                "海冰",
            ],
        }
    return {
        "found": True,
        "keyword": keyword or "",
        "data_type": data_type or "",
        "count": len(results),
        "total_available": {"pressure": len(all_pressure), "surface": len(all_surface)},
        "variables": [
            {
                "name": v["name"],
                "label": v["label"],
                "level_type": "高空（气压层）" if v["level_type"] == "pressure" else "地表",
                "dataset": v.get("dataset_label", ""),
            }
            for v in results
        ],
        "references": [
            "https://cds.climate.copernicus.eu/datasets",
        ],
    }


@register_tool(
    name="describe_cds_dataset",
    description=(
        "查询 CDS 数据集的详细信息，包括分辨率、时间范围、变量数量、使用场景、数据集之间的对比。\n"
        "不填 dataset_id 时返回所有数据集概览；指定 dataset_id 时返回该数据集的完整详情。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "dataset_id": {
                "type": "string",
                "description": "数据集 ID，不填则返回全部概览。如 reanalysis-era5-single-levels 等",
            },
        },
    },
)
async def describe_cds_dataset(dataset_id: str | None = None) -> dict:
    """Describe CDS dataset metadata — reads from local reference data."""
    from meteora.data.cds_variables import describe_cds_dataset as _describe

    return _describe(dataset_id)


@register_tool(
    name="lookup_ecmwf_parameter",
    description=(
        "从 ECMWF Parameter Database 查询单个或一类气象参数的官方定义。"
        "用户询问某个要素的准确含义、单位、paramId、shortName、GRIB 定义、"
        "或要求核对变量关系时调用。支持 CDS 变量名（如 total_precipitation、"
        "2m_temperature）、ECMWF shortName（如 tp、2t）和 paramId。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要素名、CDS 变量名或英文关键词，如 total_precipitation、2m_temperature、precipitation",
            },
            "short_name": {
                "type": "string",
                "description": "ECMWF shortName，如 tp、2t、t、z。已知 shortName 时优先填写",
            },
            "param_id": {
                "type": "integer",
                "description": "ECMWF paramId，如 total precipitation 是 228",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回多少个匹配项，默认 5",
            },
        },
    },
)
async def lookup_ecmwf_parameter(
    query: str | None = None,
    short_name: str | None = None,
    param_id: int | None = None,
    limit: int = 5,
) -> dict:
    """Lookup official ECMWF parameter definitions."""
    from meteora.data.ecmwf_params import lookup_ecmwf_parameters

    return await lookup_ecmwf_parameters(
        query=query,
        short_name=short_name,
        param_id=param_id,
        limit=limit,
    )


@register_tool(
    name="search_gfs_variables",
    description=(
        "搜索 GFS GRIB2 要素官方缩写和含义。数据来自 NCEP/NCO GRIB2 Table 4.2，"
        "支持中文/英文关键词和 GRIB short name。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "变量缩写或关键词，如 TMP、temperature、温度、降水、wind",
            },
        },
    },
)
async def search_gfs_variables(keyword: str | None = None) -> dict:
    """Search official NCEP/NCO GRIB2 parameter definitions for GFS fields."""
    from meteora.data.gfs_params import get_gfs_parameters, search_gfs_parameters
    from meteora.data.gfs_products import get_gfs_product_inventory, search_gfs_inventory

    inventory_results = []
    inventory_error = ""
    try:
        inventory = await get_gfs_product_inventory()
        inventory_results = search_gfs_inventory(inventory, keyword)
    except Exception as exc:
        inventory_error = str(exc)

    parameter_results = []
    parameter_error = ""
    try:
        parameters = await get_gfs_parameters()
        parameter_results = search_gfs_parameters(parameters, keyword)
    except Exception as exc:
        parameter_error = str(exc)

    if not inventory_results and not parameter_results:
        return {
            "found": False,
            "keyword": keyword or "",
            "message": f"未找到匹配的 GFS/GRIB2 要素：{keyword}",
            "source": "https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2.shtml",
            "inventory_error": inventory_error or None,
            "parameter_error": parameter_error or None,
        }

    return {
        "found": True,
        "keyword": keyword or "",
        "inventory_count": len(inventory_results),
        "parameter_count": len(parameter_results),
        "downloadable_in_gfs_inventory": bool(inventory_results),
        "warning": (
            "该关键词在 GRIB2 参数表中有定义，但当前 NCO GFS 产品清单中没有匹配记录。"
            "不要自动改用近似变量（例如 TMP:surface）；必须先向用户说明差异并取得确认。"
            if parameter_results and not inventory_results
            else None
        ),
        "inventory": [
            {
                "parameter": item["parameter"],
                "description": item["description"],
                "level": item["level"],
                "forecast_valid": item["forecast_valid"],
                "product": item["product"],
                "resolution": item["resolution"],
                "subset": item["subset"],
                "file_name": item["file_name"],
                "source_url": item["source_url"],
            }
            for item in inventory_results[:80]
        ],
        "variables": [
            {
                "abbrev": item["abbrev"],
                "parameter": item["parameter"],
                "units": item["units"],
                "discipline": item["discipline"],
                "category": item["category"],
                "number": item["number"],
                "source_url": item["source_url"],
            }
            for item in parameter_results[:50]
        ],
        "sources": {
            "product_inventory": "https://www.nco.ncep.noaa.gov/pmb/products/gfs/#GFS",
            "grib2_table": "https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2.shtml",
        },
    }


@register_tool(
    name="lookup_gfs_parameter",
    description=(
        "从 NCEP/NCO GRIB2 Table 4.2 查询 GFS 要素的官方定义、单位和编号。"
        "支持 GRIB short name（如 TMP、HGT、UGRD、APCP）或英文/中文关键词。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要素关键词，如 temperature、precipitation、温度",
            },
            "abbrev": {
                "type": "string",
                "description": "GRIB short name，如 TMP、HGT、UGRD、APCP",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回多少个匹配项，默认 5",
            },
        },
    },
)
async def lookup_gfs_parameter(
    query: str | None = None,
    abbrev: str | None = None,
    limit: int = 5,
) -> dict:
    """Lookup official NCEP/NCO GRIB2 parameter definitions for GFS fields."""
    from meteora.data.gfs_params import lookup_gfs_parameters
    from meteora.data.gfs_products import get_gfs_product_inventory, search_gfs_inventory

    inventory_results = []
    search_text = abbrev or query
    if search_text:
        try:
            inventory = await get_gfs_product_inventory()
            inventory_results = search_gfs_inventory(inventory, search_text)[:80]
        except Exception:
            inventory_results = []
    try:
        result = await lookup_gfs_parameters(query=query, abbrev=abbrev, limit=limit)
    except Exception as exc:
        result = {"found": False, "message": str(exc)}

    result["product_inventory_count"] = len(inventory_results)
    result["product_inventory"] = [
        {
            "parameter": item["parameter"],
            "description": item["description"],
            "level": item["level"],
            "forecast_valid": item["forecast_valid"],
            "product": item["product"],
            "resolution": item["resolution"],
            "subset": item["subset"],
            "file_name": item["file_name"],
            "source_url": item["source_url"],
        }
        for item in inventory_results
    ]
    result["sources"] = {
        "product_inventory": "https://www.nco.ncep.noaa.gov/pmb/products/gfs/#GFS",
        "grib2_table": "https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2.shtml",
    }
    if inventory_results:
        result["found"] = True
    return result


