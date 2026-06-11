"""Mode definitions and tool access control for Meteora."""

from typing import Any

MODE_ORDER = ("plan", "execute", "qa")

MODE_OPTIONS = [
    ("plan", "规划 (Plan)  — 只读查询 + 生成规划文档"),
    ("execute", "执行 (Execute) — 完整访问权限"),
    ("qa", "问答 (Q&A) — 纯只读查询"),
]

MODE_LABELS = {
    "plan": "规划",
    "execute": "执行",
    "qa": "问答",
}

MODE_BORDER_COLORS: dict[str, str] = {
    "plan": "#f39c12",
    "execute": "#5dade2",
    "qa": "#58d68d",
}

BLOCKED_IN_PLAN: set[str] = {
    "download_era5",
    "download_gfs",
    "download_ifs",
    "run_shell",
    "write_file",
    "edit_file",
    "ensure_runtime_tools",
    "delete_file",
    "configure_cds_key",
    "configure_llm_provider",
    "clear_llm_config",
    "configure_vision_model",
    "clear_cds_config",
    "retry_download",
    "cleanup_downloads",
    "download_literature_pdf",
    "download_dataset",
    "save_literature",
    "launch_sub_agent",
}

BLOCKED_IN_QA: set[str] = {
    *BLOCKED_IN_PLAN,
    "download_gefs",
    "write_plan_document",
    "propose_execution",
    "configure_email_config",
    "send_email",
    "record_instruction",
    "clear_instructions",
    "set_max_tool_rounds",
}


def is_tool_allowed(tool_name: str, mode: str) -> bool:
    if mode == "execute":
        return True
    blocker = BLOCKED_IN_QA if mode == "qa" else BLOCKED_IN_PLAN
    return tool_name not in blocker


def block_reason(tool_name: str, mode: str) -> str | None:
    if is_tool_allowed(tool_name, mode):
        return None
    if mode == "qa":
        return "当前在问答模式，不允许写文件或执行命令。用 /mode 或 Tab 切换到执行模式。"
    return "当前在规划模式，不允许写文件或执行命令。用 /mode 或 Tab 切换到执行模式。"


def filter_tool_functions(functions: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    if mode == "execute":
        return functions
    blocker = BLOCKED_IN_QA if mode == "qa" else BLOCKED_IN_PLAN
    return [f for f in functions if f.get("function", {}).get("name", "") not in blocker]
