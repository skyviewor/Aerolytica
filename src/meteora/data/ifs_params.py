"""ECMWF IFS parameter definitions, Chinese aliases, and search helpers.

IFS open data parameters are ECMWF short names.  This module provides:
- Catalog of parameters available in IFS open data by levtype
- Chinese weather term aliases for keyword search
- Search and lookup functions
"""

from __future__ import annotations

from typing import Any

IFS_SURFACE_PARAMS = {
    "10fg": {
        "name": "10 metre wind gust since previous post-processing",
        "units": "m s-1", "description": "10米阵风",
    },
    "10u": {
        "name": "10 metre U wind component",
        "units": "m s-1", "description": "10米纬向风",
    },
    "10v": {
        "name": "10 metre V wind component",
        "units": "m s-1", "description": "10米经向风",
    },
    "2d": {
        "name": "2 metre dewpoint temperature",
        "units": "K", "description": "2米露点温度",
    },
    "2t": {
        "name": "2 metre temperature",
        "units": "K", "description": "2米气温",
    },
    "asn": {"name": "Snow albedo", "units": "(0-1)", "description": "雪反照率"},
    "cape": {
        "name": "Convective available potential energy",
        "units": "J kg-1", "description": "对流有效位能",
    },
    "d2m": {
        "name": "2 metre dewpoint temperature",
        "units": "K", "description": "2米露点温度",
    },
    "dpt": {"name": "Dew point temperature", "units": "K", "description": "露点温度"},
    "e": {"name": "Evaporation", "units": "m of water", "description": "蒸发量"},
    "ewss": {
        "name": "Eastward turbulent surface stress",
        "units": "N m-2 s", "description": "东向湍流地表应力",
    },
    "gh": {"name": "Geopotential", "units": "m2 s-2", "description": "位势"},
    "hcc": {"name": "High cloud cover", "units": "(0-1)", "description": "高云量"},
    "lcc": {"name": "Low cloud cover", "units": "(0-1)", "description": "低云量"},
    "lsm": {"name": "Land-sea mask", "units": "(0-1)", "description": "海陆掩码"},
    "mcc": {"name": "Medium cloud cover", "units": "(0-1)", "description": "中云量"},
    "msl": {"name": "Mean sea level pressure", "units": "Pa", "description": "海平面气压"},
    "nsss": {
        "name": "Northward turbulent surface stress",
        "units": "N m-2 s", "description": "北向湍流地表应力",
    },
    "ptype": {"name": "Precipitation type", "units": "code", "description": "降水类型"},
    "ro": {"name": "Runoff", "units": "m", "description": "径流"},
    "rsn": {"name": "Snow density", "units": "kg m-3", "description": "雪密度"},
    "sd": {
        "name": "Snow depth",
        "units": "m of water equivalent", "description": "雪深",
    },
    "sf": {
        "name": "Snowfall",
        "units": "m of water equivalent", "description": "降雪量",
    },
    "sithick": {"name": "Sea ice thickness", "units": "m", "description": "海冰厚度"},
    "skt": {"name": "Skin temperature", "units": "K", "description": "地表温度"},
    "sp": {"name": "Surface pressure", "units": "Pa", "description": "地表气压"},
    "ssr": {
        "name": "Surface net solar radiation",
        "units": "J m-2", "description": "地表净短波辐射",
    },
    "ssrd": {
        "name": "Surface solar radiation downwards",
        "units": "J m-2", "description": "地表向下短波辐射",
    },
    "str": {
        "name": "Surface net thermal radiation",
        "units": "J m-2", "description": "地表净长波辐射",
    },
    "strd": {
        "name": "Surface thermal radiation downwards",
        "units": "J m-2", "description": "地表向下长波辐射",
    },
    "t2m": {"name": "2 metre temperature", "units": "K", "description": "2米气温"},
    "tcc": {"name": "Total cloud cover", "units": "(0-1)", "description": "总云量"},
    "tciw": {
        "name": "Total column ice water",
        "units": "kg m-2", "description": "整层冰水含量",
    },
    "tclw": {
        "name": "Total column liquid water",
        "units": "kg m-2", "description": "整层液态水含量",
    },
    "tco3": {
        "name": "Total column ozone",
        "units": "kg m-2", "description": "整层臭氧含量",
    },
    "tcwv": {
        "name": "Total column water vapour",
        "units": "kg m-2", "description": "整层水汽含量",
    },
    "tp": {"name": "Total precipitation", "units": "m", "description": "总降水"},
    "tprate": {
        "name": "Total precipitation rate",
        "units": "kg m-2 s-1", "description": "降水率",
    },
    "ttr": {"name": "Top thermal radiation", "units": "J m-2", "description": "大气顶长波辐射"},
    "vsw": {
        "name": "Volumetric soil water",
        "units": "m3 m-3", "description": "土壤体积含水量",
    },
}

IFS_PRESSURE_PARAMS = {
    "d": {"name": "Divergence", "units": "s-1", "description": "散度"},
    "gh": {"name": "Geopotential", "units": "m2 s-2", "description": "位势"},
    "q": {"name": "Specific humidity", "units": "kg kg-1", "description": "比湿"},
    "r": {"name": "Relative humidity", "units": "%", "description": "相对湿度"},
    "t": {"name": "Temperature", "units": "K", "description": "温度"},
    "u": {"name": "U wind component", "units": "m s-1", "description": "纬向风"},
    "v": {"name": "V wind component", "units": "m s-1", "description": "经向风"},
    "vo": {"name": "Vorticity (relative)", "units": "s-1", "description": "涡度"},
    "w": {"name": "Vertical velocity", "units": "Pa s-1", "description": "垂直速度"},
    "z": {"name": "Geopotential Height", "units": "m", "description": "位势高度"},
}

IFS_PRESSURE_LEVELS = [
    "1000", "925", "850", "700", "600", "500", "400",
    "300", "250", "200", "150", "100", "50", "10",
]

IFS_SOIL_PARAMS = {
    "sot": {"name": "Soil temperature", "units": "K", "description": "土壤温度"},
    "vsw": {"name": "Volumetric soil water", "units": "m3 m-3", "description": "土壤体积含水量"},
}

IFS_SOIL_LEVELS = ["1", "2", "3", "4"]

IFS_WAVE_PARAMS = {
    "swh": {
        "name": "Significant height of combined wind waves and swell",
        "units": "m", "description": "有效波高",
    },
    "mwd": {
        "name": "Mean wave direction",
        "units": "degrees", "description": "平均波向",
    },
    "mwp": {
        "name": "Mean wave period",
        "units": "s", "description": "平均波周期",
    },
    "mp2": {
        "name": "Mean zero-crossing wave period",
        "units": "s", "description": "平均跨零周期",
    },
    "pp1d": {
        "name": "Peak wave period",
        "units": "s", "description": "谱峰周期",
    },
    "cdww": {
        "name": "Coefficient of drag with waves",
        "units": "dimensionless", "description": "波浪拖曳系数",
    },
    "wmb": {
        "name": "Wave model bathymetry",
        "units": "m", "description": "波浪模式水深",
    },
    "h1012": {
        "name": "2D wave spectra (frequency bin 10-12)",
        "units": "dimensionless", "description": "二维波浪谱 (10-12)",
    },
    "h1214": {
        "name": "2D wave spectra (frequency bin 12-14)",
        "units": "dimensionless", "description": "二维波浪谱 (12-14)",
    },
    "h1417": {
        "name": "2D wave spectra (frequency bin 14-17)",
        "units": "dimensionless", "description": "二维波浪谱 (14-17)",
    },
    "h1721": {
        "name": "2D wave spectra (frequency bin 17-21)",
        "units": "dimensionless", "description": "二维波浪谱 (17-21)",
    },
    "h2125": {
        "name": "2D wave spectra (frequency bin 21-25)",
        "units": "dimensionless", "description": "二维波浪谱 (21-25)",
    },
    "h2530": {
        "name": "2D wave spectra (frequency bin 25-30)",
        "units": "dimensionless", "description": "二维波浪谱 (25-30)",
    },
}

CHINESE_ALIASES: dict[str, list[str]] = {
    "温度": ["2t", "t", "skt"],
    "气温": ["2t", "t", "t2m"],
    "2米温度": ["2t", "t2m"],
    "2米气温": ["2t", "t2m"],
    "降水": ["tp", "tprate"],
    "总降水": ["tp"],
    "降水率": ["tprate"],
    "降雪": ["sf"],
    "雪深": ["sd"],
    "风": ["10u", "10v", "u", "v"],
    "纬向风": ["10u", "u"],
    "经向风": ["10v", "v"],
    "10米风": ["10u", "10v"],
    "位势高度": ["z", "gh"],
    "位势": ["z", "gh"],
    "气压": ["msl", "sp"],
    "海平面气压": ["msl"],
    "地表气压": ["sp", "sp"],
    "地面气压": ["sp"],
    "湿度": ["q", "r"],
    "比湿": ["q"],
    "相对湿度": ["r"],
    "云量": ["tcc", "lcc", "mcc", "hcc"],
    "总云量": ["tcc"],
    "低云量": ["lcc"],
    "中云量": ["mcc"],
    "高云量": ["hcc"],
    "露点": ["2d", "d2m"],
    "露点温度": ["2d", "d2m"],
    "垂直速度": ["w"],
    "涡度": ["vo"],
    "散度": ["d"],
    "辐射": ["ssrd", "strd", "ssr", "str", "ttr"],
    "短波辐射": ["ssrd", "ssr"],
    "长波辐射": ["strd", "str", "ttr"],
    "地表温度": ["skt"],
    "土壤温度": ["sot"],
    "土壤湿度": ["vsw"],
    "蒸发": ["e"],
    "径流": ["ro"],
    "阵风": ["10fg"],
    "对流有效位能": ["cape"],
    "海冰": ["sithick"],
    "雪反照率": ["asn"],
    "雪密度": ["rsn"],
    "水汽": ["tcwv"],
    "液态水": ["tclw"],
    "冰水": ["tciw"],
    "臭氧": ["tco3"],
    "海浪": ["swh", "mwd", "mwp", "mp2", "pp1d"],
    "波浪": ["swh", "mwd", "mwp", "mp2", "pp1d"],
    "有效波高": ["swh"],
    "波高": ["swh"],
    "波向": ["mwd"],
    "平均波向": ["mwd"],
    "波周期": ["mwp", "mp2", "pp1d"],
    "平均波周期": ["mwp"],
    "跨零周期": ["mp2"],
    "谱峰周期": ["pp1d"],
    "拖曳系数": ["cdww"],
    "波浪拖曳系数": ["cdww"],
    "水深": ["wmb"],
    "波浪谱": ["h1012", "h1214", "h1417", "h1721", "h2125", "h2530"],
}

LEVTYPE_NAMES: dict[str, str] = {
    "sfc": "地表 (sfc)",
    "pl": "等压面 (pl)",
    "sol": "土壤层 (sol)",
    "sfo": "海面 (sfo)",
}


def list_ifs_parameters_by_levtype(levtype: str | None = None) -> dict[str, Any]:
    if levtype == "sfc":
        params = {**IFS_SURFACE_PARAMS, **IFS_WAVE_PARAMS}
        levels = None
    elif levtype == "pl":
        params = {**IFS_PRESSURE_PARAMS}
        levels = IFS_PRESSURE_LEVELS
    elif levtype == "sol":
        params = {**IFS_SOIL_PARAMS}
        levels = IFS_SOIL_LEVELS
    else:
        params = {**IFS_SURFACE_PARAMS, **IFS_PRESSURE_PARAMS, **IFS_SOIL_PARAMS, **IFS_WAVE_PARAMS}
        levels = None
    return {"parameters": params, "levels": levels, "levtype": levtype}


def get_ifs_levels(levtype: str | None = None) -> dict[str, Any]:
    result: dict[str, list[str] | None] = {}
    if levtype is None or levtype == "pl":
        result["pl"] = IFS_PRESSURE_LEVELS
    if levtype is None or levtype == "sol":
        result["sol"] = IFS_SOIL_LEVELS
    if levtype == "sfc" or levtype is None:
        result["sfc"] = None
    if levtype == "sfo":
        result["sfo"] = None
    return {"levels": result, "levtype": levtype}


def search_ifs_parameters(keyword: str | None) -> dict[str, Any]:
    if not keyword or not keyword.strip():
        return {
            "found": False,
            "keyword": keyword or "",
            "message": "请输入搜索关键词。",
            "levtype_hint": {**LEVTYPE_NAMES},
        }

    keyword = keyword.strip()
    results: list[dict] = []

    search_lower = keyword.lower()
    search_aliases = [
        short_name
        for alias_key, short_names in CHINESE_ALIASES.items()
        if keyword in alias_key or alias_key in keyword
        for short_name in short_names
    ]

    all_params = {
        "sfc": IFS_SURFACE_PARAMS,
        "pl": IFS_PRESSURE_PARAMS,
        "sol": IFS_SOIL_PARAMS,
        "sfo": IFS_WAVE_PARAMS,
    }

    for levtype, params in all_params.items():
        for param_name, info in params.items():
            name_lower = info["name"].lower()
            desc_lower = info["description"].lower()
            if (
                search_lower in param_name
                or search_lower in name_lower
                or search_lower in desc_lower
                or keyword in info["description"]
                or keyword in info["name"]
                or param_name in search_aliases
            ):
                results.append(
                    {
                        "param": param_name,
                        "levtype": levtype,
                        "name": info["name"],
                        "units": info["units"],
                        "description": info["description"],
                    }
                )

    if not results:
        return {
            "found": False,
            "keyword": keyword,
            "message": (
                f"未找到匹配的 IFS 要素：{keyword}。"
                "请尝试使用 ECMWF short name（如 2t、tp、z）或英文关键词搜索。"
            ),
            "levtype_hint": {**LEVTYPE_NAMES},
            "suggestion": "也可以使用 lookup_ecmwf_parameter 工具查询 ECMWF 参数库。",
        }

    return {
        "found": True,
        "keyword": keyword,
        "count": len(results),
        "parameters": results[:80],
        "levtype_hint": {**LEVTYPE_NAMES},
    }


def lookup_ifs_parameter(
    query: str | None = None,
    short_name: str | None = None,
) -> dict[str, Any]:
    search_key = (short_name or query or "").strip().lower()
    if not search_key:
        return {"found": False, "message": "请提供 query 或 short_name。"}

    all_params = {
        "sfc": IFS_SURFACE_PARAMS,
        "pl": IFS_PRESSURE_PARAMS,
        "sol": IFS_SOIL_PARAMS,
        "sfo": IFS_WAVE_PARAMS,
    }

    results: list[dict] = []
    for levtype, params in all_params.items():
        for param_name, info in params.items():
            if param_name == search_key or search_key in info["name"].lower():
                results.append(
                    {
                        "param": param_name,
                        "levtype": levtype,
                        "name": info["name"],
                        "units": info["units"],
                        "description": info["description"],
                    }
                )

    if not results:
        return {
            "found": False,
            "query": query or short_name,
            "message": f"IFS 开源数据中未找到参数：{search_key}。",
            "suggestion": "可使用 lookup_ecmwf_parameter 查询完整 ECMWF 参数库。",
        }

    return {
        "found": True,
        "query": query or short_name,
        "count": len(results),
        "parameters": results,
    }
