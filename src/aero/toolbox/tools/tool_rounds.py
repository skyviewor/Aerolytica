"""Tool-call round limit controls."""

from __future__ import annotations

from aero.toolbox.config_access import find_config, find_config_path
from aero.toolbox.registry import register_tool

_runtime_max_tool_rounds: int | None = None


@register_tool(
    name="set_max_tool_rounds",
    description=(
        "设置最大工具调用轮次上限。当用户说'把轮数调到XX'、'提高上限'、'设置轮数'时调用此工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "value": {
                "type": "integer",
                "description": "新的轮数上限，必须 >= 1",
            },
        },
        "required": ["value"],
    },
)
def set_max_tool_rounds(value: int) -> dict:
    if value < 1:
        return {"status": "error", "message": f"轮数上限必须 >= 1，接收到: {value}"}
    global _runtime_max_tool_rounds
    _runtime_max_tool_rounds = value
    config = find_config()
    config.max_tool_rounds = value
    config_path = find_config_path()
    if config_path.exists():
        config.save(config_path)
    return {
        "status": "success",
        "max_tool_rounds": value,
        "message": f"最大工具调用轮次已设置为 {value} 轮。",
    }


@register_tool(
    name="get_max_tool_rounds",
    description="查询当前最大工具调用轮次上限。用户问'当前轮数上限是多少'时调用此工具。",
    parameters={
        "type": "object",
        "properties": {},
    },
)
def get_max_tool_rounds() -> dict:
    if _runtime_max_tool_rounds is not None:
        mt = _runtime_max_tool_rounds
    else:
        config = find_config()
        mt = getattr(config, "max_tool_rounds", 999)
    return {
        "status": "success",
        "max_tool_rounds": mt,
        "message": f"当前最大工具调用轮次为 {mt} 轮。可通过 /set max_tool_rounds N 修改（如 /set max_tool_rounds 50）。",
    }
