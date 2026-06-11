"""Observation data parsing tools."""

import csv
from collections import Counter
from pathlib import Path

from meteora.data.isd_parser import parse_isd_csv as _parse_isd_csv
from meteora.toolbox.paths import resolve_project_path, short_path
from meteora.toolbox.registry import register_tool


@register_tool(
    name="parse_isd_csv",
    description=(
        "将 NOAA ISD Global Hourly 原始 CSV 解码为人类可读的常规气象要素表。"
        "兼容机场 METAR/SPECI 与普通站 SYNOP，输出气温、湿度、风、气压、"
        "能见度、云底、天气现象、降水及质量码。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "input_path": {"type": "string", "description": "输入 NOAA ISD CSV 文件路径。"},
            "output_path": {
                "type": "string",
                "description": "可选输出路径，默认在原文件旁生成 *_parsed.csv。",
            },
            "overwrite": {"type": "boolean", "default": False, "description": "是否覆盖已有输出。"},
        },
        "required": ["input_path"],
        "additionalProperties": False,
    },
)
async def parse_isd_csv(
    input_path: str,
    output_path: str | None = None,
    overwrite: bool = False,
) -> dict:
    source = resolve_project_path(input_path)
    if not source.exists():
        return {"status": "error", "message": f"文件不存在：{short_path(source)}"}
    destination = (
        resolve_project_path(output_path)
        if output_path
        else source.with_name(f"{source.stem}_parsed.csv")
    )
    if destination.resolve() == source.resolve():
        return {"status": "error", "message": "输出文件不能覆盖原始 ISD 文件"}
    if destination.exists() and not overwrite:
        return {"status": "error", "message": f"输出文件已存在：{short_path(destination)}"}
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        summary = _parse_isd_csv(source, destination)
    except (OSError, ValueError) as exc:
        return {"status": "error", "message": f"ISD 数据解析失败：{exc}"}
    return {
        "status": "success",
        "input_file": short_path(source),
        "output_file": short_path(destination),
        **summary,
    }


@register_tool(
    name="inspect_csv_table",
    description=(
        "只读检查 CSV 表格，返回行数、字段、缺测数量、数值字段的最小值、最大值、均值，"
        "以及文本字段的常见值。查询最大最小值、字段范围或数据概况时优先使用本工具，"
        "不要临时执行 Shell 或 Python 命令。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "CSV 文件路径。"},
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，仅检查指定字段；默认检查全部字段。",
            },
            "top_values": {
                "type": "integer",
                "default": 5,
                "description": "每个文本字段返回的常见值数量，范围 1-20。",
            },
        },
        "required": ["file_path"],
        "additionalProperties": False,
    },
)
async def inspect_csv_table(
    file_path: str,
    columns: list[str] | None = None,
    top_values: int = 5,
) -> dict:
    source = resolve_project_path(file_path)
    if not source.exists():
        return {"status": "error", "message": f"文件不存在：{short_path(source)}"}
    try:
        summary = _inspect_csv_table(source, columns, top_values)
    except (OSError, ValueError, csv.Error) as exc:
        return {"status": "error", "message": f"CSV 表格检查失败：{exc}"}
    return {"status": "success", "file": short_path(source), **summary}


def _inspect_csv_table(
    source: Path,
    columns: list[str] | None,
    top_values: int,
) -> dict:
    limit = max(1, min(int(top_values), 20))
    with source.open(newline="") as input_file:
        reader = csv.DictReader(input_file)
        fields = list(reader.fieldnames or ())
        if not fields:
            raise ValueError("CSV 文件没有字段")
        selected = columns or fields
        missing = [column for column in selected if column not in fields]
        if missing:
            raise ValueError(f"字段不存在：{', '.join(missing)}")
        values = {column: [] for column in selected}
        row_count = 0
        for row in reader:
            row_count += 1
            for column in selected:
                value = (row.get(column) or "").strip()
                if value:
                    values[column].append(value)

    summaries = {}
    for column in selected:
        non_empty = values[column]
        numeric_values: list[float] = []
        numeric = bool(non_empty)
        for value in non_empty:
            try:
                numeric_values.append(float(value))
            except ValueError:
                numeric = False
                break
        base = {
            "non_null": len(non_empty),
            "missing": row_count - len(non_empty),
        }
        if numeric and numeric_values:
            base.update(
                {
                    "type": "numeric",
                    "min": min(numeric_values),
                    "max": max(numeric_values),
                    "mean": sum(numeric_values) / len(numeric_values),
                }
            )
        else:
            base.update(
                {
                    "type": "text",
                    "unique": len(set(non_empty)),
                    "top_values": [
                        {"value": value, "count": count}
                        for value, count in Counter(non_empty).most_common(limit)
                    ],
                }
            )
        summaries[column] = base
    return {
        "rows": row_count,
        "columns": fields,
        "selected_columns": selected,
        "summary": summaries,
    }
