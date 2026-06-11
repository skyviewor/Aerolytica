"""User preference and instruction tools."""

from meteora.toolbox.paths import find_project_dir, short_path
from meteora.toolbox.registry import register_tool


@register_tool(
    name="record_instruction",
    description=(
        "记录用户的一条个性化指令或偏好。当用户说'记住xxx'、'以后xxx'、'默认xxx'、"
        "'我的习惯是xxx'、'不要总是xxx'等表达时，应调用此工具保存。"
        "scope='global' 表示全局偏好（跨项目生效），scope='project' 表示仅当前项目生效。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "用户想要记住的指令内容，如 '温度默认用摄氏度'。",
            },
            "scope": {
                "type": "string",
                "description": "指令作用范围：'project' 仅当前项目，'global' 跨项目。",
                "enum": ["project", "global"],
            },
        },
        "required": ["instruction"],
    },
)
async def record_instruction(instruction: str, scope: str = "project") -> dict:
    from meteora.data.instructions import append_instruction

    project_dir = find_project_dir()
    try:
        path = append_instruction(instruction, scope=scope, project_dir=project_dir)
        scope_label = "全局偏好" if scope == "global" else "项目要求"
        return {
            "success": True,
            "message": f"已记录{scope_label}：" + instruction,
            "saved_to": short_path(path),
            "scope": scope,
        }
    except (OSError, ValueError) as exc:
        return {"success": False, "error": str(exc)}


@register_tool(
    name="show_instructions",
    description=(
        "查看当前已记录的个性化指令和偏好设置。"
        "全局偏好和项目要求都会列出。不传 scope 参数时返回全部。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": "可选，只看某一类型：'project' 或 'global'",
                "enum": ["project", "global"],
            },
        },
    },
)
async def show_instructions(scope: str | None = None) -> dict:
    from meteora.data.instructions import load_instructions

    project_dir = find_project_dir()
    full_text = load_instructions(project_dir=project_dir)
    if scope:
        from meteora.data.instructions import _resolve_scope_path

        path = _resolve_scope_path(scope, project_dir=project_dir)
        text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        scope_label = "全局偏好" if scope == "global" else "项目要求"
        return {
            "success": True,
            "has_instructions": bool(text),
            "scope": scope,
            "label": scope_label,
            "instructions": text or f"暂无{scope_label}",
        }
    return {
        "success": True,
        "has_instructions": bool(full_text),
        "instructions": full_text or "暂无个性化指令。你可以说'记住xxx'来添加。",
    }


@register_tool(
    name="clear_instructions",
    description=(
        "清空已记录的个性化指令。scope='project' 清空项目要求，scope='global' 清空全局偏好。"
        "不传 scope 则清空项目级。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": "要清空的指令类型：'project' 或 'global'。默认 'project'",
                "enum": ["project", "global"],
            },
        },
    },
)
async def clear_instructions(scope: str = "project") -> dict:
    from meteora.data.instructions import clear_instructions as _clear

    project_dir = find_project_dir()
    try:
        _clear(scope=scope, project_dir=project_dir)
        scope_label = "全局偏好" if scope == "global" else "项目要求"
        return {"success": True, "message": f"已清空{scope_label}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
