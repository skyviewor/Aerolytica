"""Plan document and execution-transition tools."""

from aero.toolbox.paths import find_project_dir, short_path
from aero.toolbox.registry import register_tool


@register_tool(
    name="write_plan_document",
    description=(
        "将规划方案保存为 Markdown 文件到 plans/ 目录。每次保存都会写入同一个计划文件，"
        "方便反复调整。每个计划文件名都带时间戳。"
        "如果当前计划已被执行过（用户切到执行模式并进行了构建操作），则会自动创建新计划。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "完整的规划方案文档内容（Markdown 格式）",
            },
            "title": {
                "type": "string",
                "description": "计划标题，可选。如 '下载 ERA5 全球气温并绘制分布图'",
            },
        },
        "required": ["content"],
    },
)
async def write_plan_document(content: str, title: str = "") -> dict:
    """Write a planning document to plans/ directory."""
    from aero.data.plans import is_plan_locked, write_plan

    project_dir = find_project_dir()
    try:
        result = write_plan(content, title=title, project_dir=project_dir)
        locked = is_plan_locked(project_dir)
        return {
            "success": True,
            "saved_to": short_path(result["saved_to"]),
            "title": result["title"],
            "locked": locked,
            "message": (
                "计划书已保存。如需调整请继续修改，如需开始构建请切换到执行模式。"
                if not locked
                else "新计划已创建（上一轮计划已进入构建阶段）。"
            ),
        }
    except (OSError, ValueError) as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="propose_execution",
    description=(
        "在规划模式下，当规划方案完成后，向用户发起执行确认。"
        "调用此工具会弹出一个确认窗口，询问用户是否切换到执行模式开始构建。"
        "用户选择「开始」后系统自动切换到执行模式，AI 即可开始下载数据、运行脚本等操作。"
        "用户选择「暂不」则继续留在规划模式。"
        "**仅在规划模式（plan mode）下使用，执行模式下无需调用。**"
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
    requires_confirmation=True,
)
async def propose_execution() -> dict:
    """Propose switching to execute mode after plan is ready."""
    return {
        "approved": True,
        "message": "已切换到执行模式，开始构建执行。",
    }
