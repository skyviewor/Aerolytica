"""Managed, argument-based scientific transformations."""

from __future__ import annotations

import json
from pathlib import Path

from aero.core.runtime_paths import runtime_python_path
from aero.toolbox.paths import find_workspace_dir
from aero.toolbox.registry import register_tool


@register_tool(
    name="transform_scientific_data",
    description=(
        "使用 Aero 私有 aero-agent Python 环境处理 GRIB/NetCDF。"
        "支持 convert（转 NetCDF）、merge_time（按时间合并）、subset（规则网格区域裁剪）、"
        "mean_time、sum_time、mean_area。路径和参数不经过 Shell，适用于 Windows。"
        "输出保留变量属性及坐标；具体结果仍需检查单位、时间和元数据。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["convert", "merge_time", "subset", "mean_time", "sum_time", "mean_area"],
            },
            "input_paths": {"type": "array", "items": {"type": "string"}},
            "output_path": {"type": "string"},
            "area": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": "裁剪范围 [north, west, south, east]",
            },
            "variables": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["operation", "input_paths", "output_path"],
    },
    requires_confirmation=True,
)
async def transform_scientific_data(
    operation: str,
    input_paths: list[str],
    output_path: str,
    area: list[float] | None = None,
    variables: list[str] | None = None,
) -> dict:
    from aero.agent.runtime import Runtime

    root = find_workspace_dir().resolve()

    def local_path(value: str) -> Path:
        path = (root / value).resolve()
        if not path.is_relative_to(root):
            raise ValueError("只能处理当前项目目录内的文件")
        return path

    try:
        inputs = [local_path(value) for value in input_paths]
        output = local_path(output_path)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    if not inputs or any(not path.is_file() for path in inputs):
        return {"status": "error", "message": "输入文件不存在"}
    if output in inputs or output.suffix.lower() != ".nc":
        return {"status": "error", "message": "输出必须是新的 .nc 文件"}
    python = runtime_python_path()
    if not python.is_file():
        return {"status": "error", "message": "aero-agent Python 未准备好"}
    script = Path(__file__).resolve().parents[2] / "science" / "transform.py"
    argv = [str(python), str(script), "--operation", operation]
    for path in inputs:
        argv.extend(("--input", str(path)))
    argv.extend(("--output", str(output)))
    if area is not None:
        argv.extend(("--area", json.dumps(area)))
    if variables is not None:
        argv.extend(("--variables", json.dumps(variables)))
    result = await Runtime().run_subprocess_streaming(
        argv, str(root), timeout_ms=900_000, output_limit=20_000
    )
    if result.exit_code == -2:
        return {"status": "error", "message": "任务已取消"}
    try:
        payload = json.loads(result.stdout.splitlines()[-1])
    except (ValueError, IndexError):
        return {
            "status": "error",
            "message": result.error or result.stderr[-4000:] or "科学计算没有返回结果",
        }
    return payload
