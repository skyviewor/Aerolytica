"""Background sub-agent tools."""

from meteora.toolbox.registry import register_tool


@register_tool(
    name="launch_sub_agent",
    description=(
        "把一个耗时但步骤清晰的任务交给后台子 agent 执行。"
        "适合大文件下载、计算平均值、生成图表等用户不需要实时等待的任务。"
        "调用后立即返回 task_id，主对话可以继续。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "后台任务短标题，8 到 20 个字以内。",
            },
            "task": {
                "type": "string",
                "description": "交给子 agent 执行的完整任务说明。",
            },
            "success_criteria": {
                "type": "string",
                "description": "完成标准，例如需要保存哪些文件、返回哪些结论。",
            },
            "context_summary": {
                "type": "string",
                "description": "主对话中和该任务相关的上下文摘要。",
            },
        },
        "required": ["title", "task"],
    },
)
def launch_sub_agent(
    title: str,
    task: str,
    success_criteria: str = "",
    context_summary: str = "",
) -> dict:
    from meteora.agent.subagent import launch_subagent_from_context

    return launch_subagent_from_context(title, task, success_criteria, context_summary)


@register_tool(
    name="query_sub_agents",
    description=(
        "查询后台子 agent 任务的实时状态、最新进度、"
        "完成结果和产物路径。"
        "当用户询问后台任务、下载进度、子任务是否完成时调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "可选后台任务 ID。不传则返回全部后台任务。",
            },
        },
    },
)
def query_sub_agents(task_id: str | None = None) -> dict:
    from meteora.agent.subagent import query_subagents_from_context

    return query_subagents_from_context(task_id)


@register_tool(
    name="cancel_sub_agent",
    description=(
        "取消正在运行或等待确认的后台子 agent 任务。"
        "当用户要求取消后台任务、停止后台下载、终止子任务时调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "要取消的后台任务 ID。",
            },
        },
        "required": ["task_id"],
    },
)
def cancel_sub_agent(task_id: str) -> dict:
    from meteora.agent.subagent import cancel_subagent_from_context

    return cancel_subagent_from_context(task_id)
