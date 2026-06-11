"""Agent loop — the core orchestration of Meteora.

Supports both non-streaming and streaming modes.
"""

import asyncio
import json
import re
import shlex
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import urlparse

import structlog

from meteora.agent.llm_client import LLMClient, LLMConfig, StreamEvent
from meteora.agent.progress import ProgressReporter, use_progress_reporter
from meteora.agent.runtime import Runtime
from meteora.agent.skills import SelectedSkill, SkillSelector, render_skill_context
from meteora.agent.system_prompt import build_system_prompt
from meteora.core.config import MeteoraConfig
from meteora.core.debug_log import debug_log
from meteora.core.llm_providers import get_provider_preset
from meteora.core.types import Message, ToolCall
from meteora.data.modes import block_reason
from meteora.data.pricing import TokenTracker
from meteora.toolbox.registry import get_registry

logger = structlog.get_logger()

_DESTRUCTIVE_SHELL_PREFIXES = (
    "rm ", "cp ", "mv ", "dd ", "mkfs", "chmod ", "chown ", "kill ", "pkill ",
    "shutdown", "reboot", "halt ", "poweroff", "init ", "systemctl stop",
    "systemctl disable", "systemctl mask", "docker rm", "docker kill",
    "pip uninstall", "pip install ", "npm uninstall", "npm install ",
    "gem uninstall", "gem install ", "cargo uninstall", "cargo install",
    "apt ", "brew ", "yum ", "dnf ", "pacman ", "zypper ", "port install",
)


_SAFE_REDIRECT_TARGETS = ("/dev/null", "&1")

_MUTATING_SHELL_COMMAND_RE = re.compile(
    r"(?:^|[;&|]\s*|\n\s*)(?:sudo\s+)?(?:\S*/)?"
    r"(?:rm|cp|mv|dd|mkfs|chmod|chown|kill|pkill|shutdown|reboot|halt|poweroff|"
    r"init|apt|brew|yum|dnf|pacman|zypper|touch|mkdir|rmdir|wget|aria2c)\b"
    r"|(?:^|[;&|]\s*|\n\s*)(?:sudo\s+)?(?:\S*/)?"
    r"(?:pip|npm|gem|cargo)\s+(?:install|uninstall)\b"
    r"|(?:^|[;&|]\s*|\n\s*)(?:sudo\s+)?(?:\S*/)?"
    r"systemctl\s+(?:stop|disable|mask)\b"
    r"|(?:^|[;&|]\s*|\n\s*)(?:sudo\s+)?(?:\S*/)?docker\s+(?:rm|kill)\b"
    r"|(?:^|[;&|]\s*|\n\s*)(?:sudo\s+)?(?:\S*/)?sed\s+[^;&|\n]*\s-i(?:\s|$)"
    r"|(?:^|[;&|]\s*|\n\s*)(?:sudo\s+)?(?:\S*/)?curl\b[^;&|\n]*"
    r"(?:\s-o(?:\s|$)|\s--output(?:=|\s))",
    re.IGNORECASE,
)

_MUTATING_PYTHON_RE = re.compile(
    r"\.(?:write_text|write_bytes|to_csv|to_excel|to_netcdf|"
    r"to_parquet|unlink|remove|rename|replace|mkdir|rmdir|touch)\s*\("
    r"|(?:^|[^\w.])(?:open|Path\.open)\s*\([^,\n]+,\s*[\"'][^\"']*[wax+][^\"']*[\"']"
    r"|(?:^|[^\w.])(?:os\.system|subprocess\.|shutil\.)",
    re.IGNORECASE | re.MULTILINE,
)

_BUILD_TOOLS: set[str] = {
    "download_era5",
    "subset_netcdf",
    "download_gfs",
    "download_ifs",
    "download_gefs",
    "ensure_runtime_tools",
    "run_shell",
    "write_file",
    "edit_file",
    "delete_file",
    "write_plan_document",
}


def _without_heredoc_bodies(command: str) -> str:
    lines = command.splitlines()
    visible: list[str] = []
    delimiter: str | None = None
    for line in lines:
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            continue
        visible.append(line)
        match = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if match:
            delimiter = match.group(1)
    return "\n".join(visible)


def _has_unsafe_redirection(command: str) -> bool:
    shell_text = _without_heredoc_bodies(command)
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(shell_text):
        char = shell_text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        if char == ">":
            remainder = shell_text[index + 1 :].lstrip("> ")
            target = remainder.split(maxsplit=1)[0].rstrip(";|&") if remainder else ""
            if target not in _SAFE_REDIRECT_TARGETS:
                return True
        index += 1
    return bool(re.search(r"\|\s*tee(?:\s|$)", shell_text))


def _existing_mkdir_is_noop(command: str, workdir: str = ".") -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens or Path(tokens[0]).name != "mkdir":
        return False

    targets: list[str] = []
    parents = False
    options_done = False
    for token in tokens[1:]:
        if not options_done and token == "--":
            options_done = True
            continue
        if not options_done and token.startswith("-"):
            if token == "--parents" or set(token[1:]) == {"p"}:
                parents = True
                continue
            return False
        targets.append(token)
    if not parents or not targets:
        return False

    from meteora.toolbox.paths import find_project_dir

    project_dir = find_project_dir().resolve()
    base = Path(workdir).expanduser()
    if not base.is_absolute():
        base = project_dir / base
    if not base.is_dir():
        base = project_dir
    return all(
        (Path(target).expanduser() if Path(target).expanduser().is_absolute() else base / target).is_dir()
        for target in targets
    )


def _is_safe_shell_command(command: str, workdir: str = ".") -> bool:
    stripped = command.strip()
    if not stripped:
        return True
    if _existing_mkdir_is_noop(stripped, workdir):
        return True
    if any(stripped.startswith(p) for p in _DESTRUCTIVE_SHELL_PREFIXES):
        return False
    if _MUTATING_SHELL_COMMAND_RE.search(_without_heredoc_bodies(stripped)):
        return False
    if _MUTATING_PYTHON_RE.search(stripped):
        return False
    if _has_unsafe_redirection(stripped):
        return False
    return True


def _runtime_tools_already_ready(args: dict) -> bool:
    try:
        from meteora.toolbox.runtime_manager import get_runtime_tool_manager

        manager = get_runtime_tool_manager()
        requested = [
            str(tool).strip()
            for tool in args.get("tools", [])
            if str(tool).strip()
        ]
        if not requested:
            return True
        unknown = [tool for tool in requested if tool not in manager.packages]
        if unknown:
            return True
        env = Runtime._build_exec_env()
        ready, _missing, _verified = manager.tools_ready(requested, env)
        return ready
    except Exception as e:
        debug_log("agent.runtime_tools_ready_check_failed", error=str(e))
        return False


def _tool_call_needs_confirmation(tool_name: str, args: dict) -> bool:
    if tool_name == "run_shell":
        return not _is_safe_shell_command(
            str(args.get("command", "")),
            str(args.get("workdir") or "."),
        )
    if tool_name == "ensure_runtime_tools":
        return not _runtime_tools_already_ready(args)
    return True


def _is_content_filter_error(error: Exception) -> bool:
    msg = str(error)
    return (
        "Content Exists Risk" in msg
        or "content_filter" in msg.lower()
        or "safety" in msg.lower()
    )

_TOOL_NAME_REPLACEMENTS = {
    "download_era5": "下载数据",
    "search_datasets": "搜索数据集目录",
    "search_dataset_variables": "查询数据集变量",
    "search_dataset_stations": "查询数据集站点",
    "describe_dataset": "查看数据集信息",
    "download_dataset": "下载数据集",
    "parse_isd_csv": "解析地面观测数据",
    "inspect_csv_table": "查看表格数据概况",
    "check_era5_availability": "检查 ERA5 数据源可用性",
    "subset_netcdf": "裁剪 NetCDF 文件",
    "inspect_nc": "查看文件详情",
    "inspect_grib2": "查看 GRIB2 文件详情",
    "search_cds_variables": "查询可用变量",
    "download_gfs": "下载 GFS 预报数据",
    "get_gfs_forecast_schedule": "解析 GFS 预报时效",
    "check_gfs_availability": "检查 GFS 可用时次",
    "inspect_gfs_inventory": "查看 GFS 文件库存",
    "search_gfs_variables": "查询 GFS 可用要素",
    "lookup_gfs_parameter": "查询 GFS 要素定义",
    "lookup_ecmwf_parameter": "查询 ECMWF 参数定义",
    "get_gefs_forecast_schedule": "解析 GEFS 预报时效",
    "check_gefs_availability": "检查 GEFS 可用时次",
    "download_gefs": "下载 GEFS 集合预报数据",
    "search_gefs_variables": "查询 GEFS 可用要素",
    "lookup_gefs_parameter": "查询 GEFS 要素定义",
    "download_ifs": "下载 IFS 预报数据",
    "get_ifs_forecast_schedule": "解析 IFS 预报时效",
    "check_ifs_availability": "检查 IFS 可用时次",
    "search_ifs_variables": "查询 IFS 可用要素",
    "configure_cds_key": "保存 CDS 凭证",
    "configure_email_config": "配置邮箱",
    "check_email_config": "检查邮箱配置",
    "send_email": "发送邮件",
    "list_downloads": "查看下载记录",
    "retry_download": "继续下载",
    "cleanup_downloads": "清理下载记录",
    "delete_file": "删除文件",
    "list_figures": "查看图片列表",
    "list_llm_providers": "查看模型服务商",
    "configure_llm_provider": "配置模型服务",
    "clear_llm_config": "清除模型服务密钥",
    "ensure_runtime_tools": "配置运行时命令行工具",
    "check_vision_model_config": "检查视觉模型配置",
    "analyze_image": "分析图片",
    "configure_vision_model": "配置视觉模型",
    "search_literature": "检索学术文献",
    "save_literature": "保存文献信息",
    "download_literature_pdf": "下载文献全文",
    "list_literature": "查看文献列表",
    "read_pdf": "提取 PDF 文本",
    "preview_image": "打开图片预览",
    "get_max_tool_rounds": "查询工具轮次上限",
    "set_max_tool_rounds": "设置工具轮次上限",
    "launch_sub_agent": "转交后台任务",
    "query_sub_agents": "查询后台任务状态",
    "cancel_sub_agent": "取消后台任务",
    "record_instruction": "记录个性化指令",
    "show_instructions": "查看已有指令",
    "clear_instructions": "清空指令记录",
    "write_plan_document": "保存计划书",
    "propose_execution": "确认执行",
}

_TOOL_PROGRESS_MESSAGES = {
    "download_era5": ("准备下载数据", "数据下载步骤已完成", "数据下载失败"),
    "search_datasets": ("正在搜索数据集目录", "数据集目录搜索完成", "搜索数据集目录失败"),
    "search_dataset_variables": ("正在查询数据集变量", "数据集变量查询完成", "查询数据集变量失败"),
    "search_dataset_stations": ("正在查询数据集站点", "数据集站点查询完成", "查询数据集站点失败"),
    "describe_dataset": ("正在查看数据集信息", "数据集信息查看完成", "查看数据集信息失败"),
    "download_dataset": ("准备下载数据集", "数据集下载完成", "数据集下载失败"),
    "parse_isd_csv": ("正在解析地面观测数据", "地面观测数据解析完成", "地面观测数据解析失败"),
    "inspect_csv_table": ("正在查看表格数据概况", "表格数据概况查看完成", "查看表格数据概况失败"),
    "check_era5_availability": (
        "正在检查 ERA5 数据源可用性",
        "ERA5 数据源可用性检查完成",
        "ERA5 数据源可用性检查失败",
    ),
    "subset_netcdf": ("正在裁剪 NetCDF 文件", "NetCDF 文件裁剪完成", "NetCDF 文件裁剪失败"),
    "inspect_nc": ("正在查看文件内容", "文件内容查看完成", "查看文件内容失败"),
    "inspect_grib2": (
        "正在查看 GRIB2 文件内容",
        "GRIB2 文件内容查看完成",
        "查看 GRIB2 文件内容失败",
    ),
    "search_cds_variables": ("正在查询可用变量", "可用变量查询完成", "查询可用变量失败"),
    "download_gfs": (
        "准备下载 GFS 预报数据",
        "GFS 预报数据下载完成",
        "GFS 预报数据下载失败",
    ),
    "get_gfs_forecast_schedule": (
        "正在解析 GFS 预报时效",
        "GFS 预报时效解析完成",
        "GFS 预报时效解析失败",
    ),
    "check_gfs_availability": (
        "正在检查 GFS 可用时次",
        "GFS 可用时次检查完成",
        "GFS 可用时次检查失败",
    ),
    "inspect_gfs_inventory": (
        "正在查看 GFS 文件库存",
        "GFS 文件库存查看完成",
        "查看 GFS 文件库存失败",
    ),
    "search_gfs_variables": (
        "正在查询 GFS 可用要素",
        "GFS 可用要素查询完成",
        "查询 GFS 可用要素失败",
    ),
    "lookup_gfs_parameter": (
        "正在查阅 GFS 要素定义",
        "GFS 要素定义查询完成",
        "查询 GFS 要素定义失败",
    ),
    "lookup_ecmwf_parameter": (
        "正在查阅 ECMWF 参数定义",
        "ECMWF 参数定义查询完成",
        "查询 ECMWF 参数定义失败",
    ),
    "get_gefs_forecast_schedule": (
        "正在解析 GEFS 预报时效",
        "GEFS 预报时效解析完成",
        "GEFS 预报时效解析失败",
    ),
    "check_gefs_availability": (
        "正在检查 GEFS 可用时次",
        "GEFS 可用时次查检完成",
        "GEFS 可用时次查检失败",
    ),
    "download_gefs": (
        "准备下载 GEFS 集合预报数据",
        "GEFS 集合预报数据下载完成",
        "GEFS 集合预报数据下载失败",
    ),
    "search_gefs_variables": (
        "正在查询 GEFS 可用要素",
        "GEFS 可用要素查询完成",
        "查询 GEFS 可用要素失败",
    ),
    "lookup_gefs_parameter": (
        "正在查阅 GEFS 要素定义",
        "GEFS 要素定义查询完成",
        "查询 GEFS 要素定义失败",
    ),
    "download_ifs": (
        "准备下载 IFS 预报数据",
        "IFS 预报数据下载完成",
        "IFS 预报数据下载失败",
    ),
    "get_ifs_forecast_schedule": (
        "正在解析 IFS 预报时效",
        "IFS 预报时效解析完成",
        "IFS 预报时效解析失败",
    ),
    "check_ifs_availability": (
        "正在检查 IFS 可用时次",
        "IFS 可用时次检查完成",
        "IFS 可用时次检查失败",
    ),
    "search_ifs_variables": (
        "正在查询 IFS 可用要素",
        "IFS 可用要素查询完成",
        "查询 IFS 可用要素失败",
    ),
    "configure_cds_key": ("正在保存 CDS 凭证", "CDS 凭证已保存", "保存 CDS 凭证失败"),
    "configure_email_config": ("正在保存邮箱配置", "邮箱配置已保存", "保存邮箱配置失败"),
    "check_email_config": ("正在检查邮箱配置", "邮箱配置检查完成", "检查邮箱配置失败"),
    "send_email": ("正在发送邮件", "邮件已发送", "邮件发送失败"),
    "list_downloads": ("正在查看下载记录", "下载记录查看完成", "查看下载记录失败"),
    "retry_download": ("开始重试下载", "重试下载已完成", "重试下载失败"),
    "cleanup_downloads": ("正在清理下载记录", "下载记录清理完成", "清理下载记录失败"),
    "delete_file": ("正在删除文件", "文件删除完成", "删除文件失败"),
    "list_llm_providers": ("正在查看可用模型服务商", "可用模型服务商已列出", "查看模型服务商失败"),
    "configure_llm_provider": ("正在配置模型服务", "模型服务配置完成", "配置模型服务失败"),
    "clear_llm_config": ("正在清除模型服务密钥", "模型服务密钥已清除", "清除模型服务密钥失败"),
    "read_file": ("正在读取文件", "文件读取完成", "读取文件失败"),
    "write_file": ("正在写入文件", "文件写入完成", "写入文件失败"),
    "edit_file": ("正在编辑文件", "文件编辑完成", "编辑文件失败"),
    "ensure_runtime_tools": (
        "正在配置运行时命令行工具",
        "运行时命令行工具配置完成",
        "运行时命令行工具配置失败",
    ),
    "run_shell": ("正在执行命令", "命令执行完成", "命令执行失败"),
    "list_files": ("正在查看文件列表", "文件列表查看完成", "查看文件列表失败"),
    "list_figures": ("正在查看图片列表", "图片列表查看完成", "查看图片列表失败"),
    "check_cds_config": ("正在检查 CDS 配置", "CDS 配置检查完成", "检查 CDS 配置失败"),
    "describe_cds_dataset": ("正在查看数据集信息", "数据集信息查看完成", "查看数据集信息失败"),
    "query_download": ("正在查询下载状态", "下载状态查询完成", "查询下载状态失败"),
    "check_vision_model_config": (
        "正在检查视觉模型配置",
        "视觉模型配置检查完成",
        "检查视觉模型配置失败",
    ),
    "analyze_image": ("正在调用视觉模型分析图片", "图片分析完成", "图片分析失败"),
    "configure_vision_model": ("正在配置视觉模型", "视觉模型配置完成", "视觉模型配置失败"),
    "search_literature": ("正在检索学术文献", "学术文献检索完成", "学术文献检索失败"),
    "save_literature": ("正在保存文献信息", "文献信息保存完成", "文献信息保存失败"),
    "download_literature_pdf": ("正在下载文献全文", "文献全文下载完成", "文献全文下载失败"),
    "list_literature": ("正在查看文献列表", "文献列表查看完成", "查看文献列表失败"),
    "read_pdf": ("正在提取 PDF 文本内容", "PDF 文本提取完成", "PDF 文本提取失败"),
    "preview_image": ("正在打开图片", "图片预览已打开", "打开图片失败"),
    "launch_sub_agent": ("正在转交后台任务", "后台任务已启动", "后台任务转交失败"),
    "query_sub_agents": (
        "正在查询后台任务状态",
        "后台任务状态查询完成",
        "后台任务状态查询失败",
    ),
    "cancel_sub_agent": ("正在取消后台任务", "后台任务已取消", "后台任务取消失败"),
    "record_instruction": ("正在记录个性化指令", "个性化指令已记录", "记录指令失败"),
    "show_instructions": ("正在查看已有指令", "已有指令查看完成", "查看指令失败"),
    "clear_instructions": ("正在清空指令记录", "指令记录已清空", "清空指令失败"),
    "write_plan_document": ("正在保存计划书", "计划书已保存", "计划书保存失败"),
}


def _short_shell_command(command: object, max_chars: int = 180) -> str:
    text = " ".join(str(command or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _short_arg_path(path: object, max_chars: int = 96) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    parts = text.split("/")
    if len(parts) > 3:
        text = "/".join(parts[-3:])
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _tool_progress_message(tool_name: str, stage: str, args: dict | None = None) -> str:
    if tool_name == "run_shell" and args is not None:
        raw_command = str(args.get("command") or "")
        try:
            from meteora.toolbox.tools.runtime import _normalize_shell_context

            raw_command, _workdir, _correction = _normalize_shell_context(
                raw_command,
                str(args.get("workdir") or "."),
            )
        except Exception:
            pass
        command = _short_shell_command(raw_command)
        if command:
            if stage == "done":
                return "命令执行完成"
            prefix = {
                "start": "正在执行命令",
                "error": "命令执行失败",
            }[stage]
            return f"{prefix}：{command}"

    if tool_name == "write_file" and args is not None:
        path = _short_arg_path(args.get("file_path"))
        if path:
            if stage == "done":
                return "文件写入完成"
            prefix = {
                "start": "正在写入文件",
                "error": "文件写入失败",
            }[stage]
            return f"{prefix}：{path}"

    messages = _TOOL_PROGRESS_MESSAGES.get(tool_name)
    if messages is not None:
        index = {"start": 0, "done": 1, "error": 2}[stage]
        return messages[index]
    label = _TOOL_NAME_REPLACEMENTS.get(tool_name, "执行当前步骤")
    if stage == "start":
        return f"正在{label}"
    if stage == "done":
        return f"{label}完成"
    return f"{label}失败"


def _tool_result_has_error_status(result: object) -> bool:
    return isinstance(result, dict) and result.get("status") == "error"


def _sanitize_user_facing_text(text: str) -> str:
    """Hide internal tool/function names from text shown to users."""
    if not text:
        return text

    text = re.sub(
        r"(?:可以)?(?:用|使用|调用|运行|执行)\s*`?inspect_nc`?\s*"
        r"(?:工具|函数)?\s*(?:来)?\s*(?:查看|检查)",
        "可以让我继续查看",
        text,
    )
    text = re.sub(
        r"(?:can|may)\s+(?:use|run|call)\s+`?inspect_nc`?\s+"
        r"(?:to\s+)?(?:inspect|check|view)",
        "can ask me to continue checking",
        text,
        flags=re.IGNORECASE,
    )

    for tool_name, replacement in _TOOL_NAME_REPLACEMENTS.items():
        text = re.sub(
            rf"(?:你)?可以通过\s*`?{tool_name}`?\s*(?:来)?\s*"
            r"(?P<verb>查看|检查|检索|下载|保存|分析)?",
            lambda match: (
                f"你可以让我{match.group('verb')}"
                if match.group("verb")
                else f"你可以让我{replacement}"
            ),
            text,
        )
        text = re.sub(
            rf"可以(?:用|使用|调用|运行|执行)\s*`?{tool_name}`?\s*(?:工具|函数)?",
            f"可以让我继续{replacement}",
            text,
        )
        text = re.sub(
            rf"(?:用|使用|调用|运行|执行)\s*`?{tool_name}`?\s*(?:工具|函数)?",
            replacement,
            text,
        )
        text = text.replace(f"`{tool_name}`", replacement)
        text = text.replace(tool_name, replacement)
    return text


class _StreamingTextSanitizer:
    """Hold a short tail so split tool names don't leak during streaming."""

    def __init__(self) -> None:
        self._pending = ""
        self._hold_chars = max(96, max(len(name) for name in _TOOL_NAME_REPLACEMENTS) + 32)

    def push(self, text: str) -> str:
        if not text:
            return ""
        self._pending += text
        if len(self._pending) <= self._hold_chars:
            return ""
        ready = self._pending[:-self._hold_chars]
        self._pending = self._pending[-self._hold_chars:]
        return _sanitize_user_facing_text(ready)

    def flush(self) -> str:
        ready = _sanitize_user_facing_text(self._pending)
        self._pending = ""
        return ready


def _sanitize_progress_text(text: str) -> str:
    """Hide internal tool/function names from progress shown in the TUI."""
    def replace_tool_status(match: re.Match[str]) -> str:
        prefix = match.group(1)
        tool_name = match.group(2).strip()
        if prefix.startswith("调用"):
            return _tool_progress_message(tool_name, "start")
        if prefix.startswith("工具完成"):
            return _tool_progress_message(tool_name, "done")
        return _tool_progress_message(tool_name, "error")

    text = re.sub(
        r"(调用工具|工具完成|工具失败)\s*[:：]\s*([A-Za-z_][A-Za-z0-9_]*)",
        replace_tool_status,
        text,
    )
    text = re.sub(r"重新提交\s*`?download_era5`?", "重新提交下载", text)
    text = _sanitize_user_facing_text(text)
    text = text.replace("重新提交 下载数据", "重新提交下载")
    text = text.replace("检测到后续工具调用，继续执行...", "继续处理后续步骤...")
    return text


_REF_KEYS = ("references", "source", "source_url", "sources")


def _collect_ref_urls(result: object) -> list[str]:
    urls: list[str] = []

    def walk(value: object, *, collect_all_urls: bool = False, depth: int = 0) -> None:
        if isinstance(value, str):
            if collect_all_urls and _is_url(value):
                urls.append(value)
            return
        if isinstance(value, list):
            for item in value:
                walk(item, collect_all_urls=collect_all_urls, depth=depth + 1)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                # Treat top-level reference fields as citation sources. Nested
                # source_url fields often appear on every search result row and
                # can flood the reply with many nearly identical links.
                is_ref_field = key in _REF_KEYS and (depth == 0 or collect_all_urls)
                walk(item, collect_all_urls=collect_all_urls or is_ref_field, depth=depth + 1)

    walk(result)
    seen: set[str] = set()
    deduped: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def _inject_refs_if_missing(text: str, ref_urls: list[str]) -> str:
    if not ref_urls:
        return text
    if "参考资料" in text or "References" in text.lower():
        return text
    lines = ["\n\n参考资料"]
    label_counts: dict[str, int] = {}
    for url in ref_urls:
        label = _reference_label(url)
        label_counts[label] = label_counts.get(label, 0) + 1
        display_label = label if label_counts[label] == 1 else f"{label} #{label_counts[label]}"
        lines.append(f"- [{display_label}]({url})")
    return text + "\n".join(lines)


def _direct_tool_response(tool_name: str, result: object) -> str | None:
    if tool_name != "analyze_image" or not isinstance(result, dict):
        return None
    if result.get("status") != "not_configured":
        return None
    message = result.get("message")
    return message if isinstance(message, str) and message.strip() else None


def _reference_label(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "codes.ecmwf.int" in host and "param" in path:
        return "ECMWF Parameter Database"
    if "nco.ncep.noaa.gov" in host and "grib2_table4-2" in path:
        return "NCO GRIB2 Table 4.2"
    if "nco.ncep.noaa.gov" in host and "/products/gfs" in path:
        return "NCO GFS Product Inventory"
    if "cds.climate.copernicus.eu" in host and "/datasets" in path:
        return "CDS Datasets"
    if "cds.climate.copernicus.eu" in host:
        return "Copernicus CDS"
    if "registry.opendata.aws" in host:
        return "AWS Open Data Registry"
    if host:
        return host.removeprefix("www.")
    return "Reference"


def _ensure_cnmaps_when_sciplot_active(
    selected: list[SelectedSkill],
    *,
    skill_selector: SkillSelector,
) -> None:
    names = {item.skill.name for item in selected}
    if "scientific-plotting" not in names or "cnmaps" in names:
        return
    all_skills = skill_selector.loader.load()
    cnmaps = next((s for s in all_skills if s.name == "cnmaps"), None)
    if cnmaps:
        selected.append(SelectedSkill(skill=cnmaps, score=100))


class AgentLoop:
    def __init__(self, config: MeteoraConfig):
        self.config = config

        llm_cfg = LLMConfig(
            provider=config.llm.provider,
            model=config.llm.model,
            reasoning_effort=config.llm.reasoning_effort,
            api_key=config.llm.active_api_key(),
            base_url=config.llm.base_url,
        )
        self.llm = LLMClient(llm_cfg)
        self.runtime = Runtime()
        self.registry = get_registry()
        self.skill_selector = SkillSelector()

        from meteora.data.instructions import load_instructions
        instructions = load_instructions(project_dir=self.config.output.data_dir)
        system = build_system_prompt(config, config.language, instructions_context=instructions)
        self.messages: list[Message] = [
            Message(role="system", content=system),
        ]
        self._cancel_event: asyncio.Event | None = None
        self.max_tool_rounds = config.max_tool_rounds
        self.always_allow: set[str] = set()
        self.confirm_future: asyncio.Future[str] | None = None
        self._ref_urls: list[str] = []
        self._direct_response: str | None = None
        self.tracker = TokenTracker()

    def reset_system_prompt(self, language: str) -> None:
        self.config.language = language
        from meteora.data.instructions import load_instructions
        instructions = load_instructions(project_dir=self.config.output.data_dir)
        system = build_system_prompt(self.config, language, instructions_context=instructions)
        self.messages[0] = Message(role="system", content=system)

    def _refresh_system_prompt_for_turn(self, user_message: str) -> None:
        selected = self.skill_selector.select(user_message)
        _ensure_cnmaps_when_sciplot_active(selected, skill_selector=self.skill_selector)
        skill_context = render_skill_context(selected)
        from meteora.data.instructions import load_instructions
        instructions = load_instructions(project_dir=self.config.output.data_dir)
        system = build_system_prompt(
            self.config, self.config.language, skill_context, instructions,
        )
        self.messages[0] = Message(role="system", content=system)
        if selected:
            debug_log(
                "agent.skills_selected",
                skills=[item.skill.name for item in selected],
            )

    def _allowed_tools(self) -> list[dict]:
        from meteora.data.modes import filter_tool_functions
        return filter_tool_functions(self.registry.list_functions(), self.config.mode)

    async def close(self):
        await self.llm.close()

    async def run(self, user_message: str) -> str:
        """Process a user message and return the agent's response (non-streaming)."""
        debug_log("agent.run_started", mode="non_stream", text_length=len(user_message))
        self._ref_urls.clear()
        self._direct_response = None
        self._drop_incomplete_tool_call_tail()
        self.messages = _sanitize_tool_message_sequence(self.messages)
        self._refresh_system_prompt_for_turn(user_message)
        self.messages.append(Message(role="user", content=user_message))

        tools = self._allowed_tools()

        try:
            text, tool_calls = await self.llm.chat_with_tools(self.messages, tools)
            self.tracker.add_llm(self.llm.last_usage)
            debug_log(
                "agent.llm_response",
                mode="non_stream",
                text_length=len(text or ""),
                tool_calls=len(tool_calls),
            )

            if tool_calls:
                response = await self._handle_tool_calls(tool_calls, text)
                debug_log("agent.run_completed", mode="non_stream", response_length=len(response))
                return response

            visible_text = _sanitize_user_facing_text(text)
            self.messages.append(Message(role="assistant", content=visible_text))
            debug_log(
                "agent.run_completed",
                mode="non_stream",
                response_length=len(visible_text),
            )
            return visible_text

        except Exception as e:
            logger.error("agent.run.error", error=str(e))
            debug_log("agent.run_error", mode="non_stream", error=repr(e))
            error_msg = f"抱歉，出错了：{e}"
            self.messages.append(Message(role="assistant", content=error_msg))
            return error_msg

    async def run_stream(
        self, user_message: str
    ) -> AsyncGenerator[StreamEvent, None]:
        """Process a user message with streaming output."""
        debug_log("agent.run_started", mode="stream", text_length=len(user_message))
        self._cancel_event = asyncio.Event()
        self._ref_urls.clear()
        self._direct_response = None
        self._drop_incomplete_tool_call_tail()
        self.messages = _sanitize_tool_message_sequence(self.messages)
        self._refresh_system_prompt_for_turn(user_message)
        message_start = len(self.messages)
        self.messages.append(Message(role="user", content=user_message))
        tools = self._allowed_tools()

        try:
            accumulated_text = ""
            tool_calls: list[ToolCall] = []
            text_sanitizer = _StreamingTextSanitizer()

            async for event in self.llm.chat_with_tools_stream(self.messages, tools):
                if event.type == "text":
                    content = text_sanitizer.push(event.content)
                    if content:
                        accumulated_text += content
                        debug_log("agent.stream_text", content_length=len(content))
                        yield StreamEvent(type="text", content=content)
                elif event.type == "tool_call":
                    tool_calls.append(event.tool_call)
                    debug_log("agent.stream_tool_call", tool_name=event.tool_call.name)
                elif event.type == "done":
                    self.tracker.add_llm(event.usage)

            tail = text_sanitizer.flush()
            if tail:
                accumulated_text += tail
                debug_log("agent.stream_text", content_length=len(tail), flushed=True)
                yield StreamEvent(type="text", content=tail)

            if tool_calls:
                async for event in self._handle_tool_calls_stream(
                    tool_calls,
                    accumulated_text,
                ):
                    yield event
            else:
                self.messages.append(Message(role="assistant", content=accumulated_text))
                debug_log(
                    "agent.run_completed",
                    mode="stream",
                    response_length=len(accumulated_text),
                )

            yield StreamEvent(type="done")

        except asyncio.CancelledError:
            debug_log("agent.run_cancelled", mode="stream")
            self.messages = self.messages[:message_start]
            yield StreamEvent(type="status", content="对话已中断，已回滚本轮上下文")
            yield StreamEvent(type="done")
            raise

        except Exception as e:
            logger.error("agent.run.error", error=str(e))
            debug_log("agent.run_error", mode="stream", error=repr(e))
            if _is_content_filter_error(e):
                self.messages = self.messages[:message_start]
                yield StreamEvent(
                    type="content_blocked",
                    content=(
                        "当前消息触发了模型服务商的内容安全拦截，"
                        "本轮对话已被排除在上下文之外，不会影响后续对话。\n"
                        "请换一种表述方式重试，或用 /provider 切换到其他服务商。"
                    ),
                )
            else:
                error_msg = f"抱歉，出错了：{e}"
                self.messages.append(Message(role="assistant", content=error_msg))
                yield StreamEvent(type="text", content=error_msg)
            yield StreamEvent(type="done")
        finally:
            if self._cancel_event is not None and self._cancel_event.is_set():
                self.messages = self.messages[:message_start]
            self._cancel_event = None

    def cancel(self) -> None:
        debug_log("agent.cancel_requested", has_cancel_event=self._cancel_event is not None)
        if self._cancel_event is not None:
            self._cancel_event.set()

    def _drop_incomplete_tool_call_tail(self) -> None:
        if self.messages and self.messages[-1].role == "assistant" and self.messages[-1].tool_calls:
            self.messages.pop()

    async def _handle_tool_calls(
        self, tool_calls: list[ToolCall], text: str
    ) -> str:
        """Execute tool calls and feed results back to LLM for natural language response."""
        self.messages.append(
            Message(role="assistant", content=text or "", tool_calls=tool_calls)
        )

        groups: dict[str, list[ToolCall]] = {}
        for tc in tool_calls:
            groups.setdefault(tc.name, []).append(tc)

        for tool_name, calls in groups.items():
            spec = self.registry.get(tool_name)
            if spec is None:
                err = f"当前操作不可用：{_sanitize_user_facing_text(tool_name)}"
                for tc in calls:
                    self.messages.append(Message(role="tool", content=err, tool_call_id=tc.id))
                continue

            reason = block_reason(tool_name, self.config.mode)
            if reason is not None:
                for tc in calls:
                    self.messages.append(Message(role="tool", content=reason, tool_call_id=tc.id))
                continue

            if spec.requires_confirmation and tool_name not in self.always_allow:
                all_args = [self._parse_tool_args(tc) for tc in calls]
                _needs_confirm = any(
                    _tool_call_needs_confirmation(tool_name, args)
                    for args in all_args
                )
                if _needs_confirm:
                    if len(calls) == 1:
                        choice = await self._ask_confirmation_simple(tool_name, all_args[0])
                    else:
                        choice = await self._ask_confirmation_simple_batch(tool_name, all_args)
                    if choice == "always":
                        self.always_allow.add(tool_name)
                    elif choice == "deny":
                        for tc in calls:
                            if tool_name == "propose_execution":
                                deny_msg = json.dumps(
                                    {"approved": False, "message": "用户选择暂不执行，继续留在规划模式完善方案。"},
                                    ensure_ascii=False,
                                )
                            else:
                                deny_msg = json.dumps({"error": "用户拒绝执行此操作"}, ensure_ascii=False)
                            self.messages.append(
                                Message(role="tool", content=deny_msg, tool_call_id=tc.id)
                            )
                        continue

            for tc in calls:
                parsed_args = self._parse_tool_args(tc)
                exec_result = await self.runtime.execute(spec.function, parsed_args)
                if exec_result.success:
                    self._apply_runtime_config_update(tc.name, exec_result.result)
                    if self.config.mode == "execute" and tc.name in _BUILD_TOOLS:
                        from meteora.data.plans import lock_current_plan
                        lock_current_plan(project_dir=self.config.output.data_dir)
                    direct_response = _direct_tool_response(tc.name, exec_result.result)
                    if direct_response:
                        self._direct_response = direct_response
                    for url in _collect_ref_urls(exec_result.result):
                        if url not in self._ref_urls:
                            self._ref_urls.append(url)
                result_str = (
                    json.dumps(exec_result.result, ensure_ascii=False, default=str)
                    if exec_result.success
                    else json.dumps({"error": exec_result.error}, ensure_ascii=False)
                )
                self.messages.append(Message(role="tool", content=result_str, tool_call_id=tc.id))

        from meteora.toolbox.builtin_tools import get_vision_usage, reset_vision_usage
        vision_usage = get_vision_usage()
        if vision_usage:
            self.tracker.add_vision(vision_usage)
            reset_vision_usage()

        if self._direct_response:
            response = self._direct_response
            self.messages.append(Message(role="assistant", content=response))
            return response

        response = _sanitize_user_facing_text(await self.llm.chat(self.messages))
        self.tracker.add_llm(self.llm.last_usage)
        response = _inject_refs_if_missing(response, self._ref_urls)
        self.messages.append(Message(role="assistant", content=response))
        return response

    async def _handle_tool_calls_stream(
        self,
        tool_calls: list[ToolCall],
        text: str,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Execute tool calls while streaming progress events."""
        self.messages.append(
            Message(role="assistant", content=text or "", tool_calls=tool_calls)
        )

        pending_calls = tool_calls
        tools = self._allowed_tools()

        for round_index in range(self.max_tool_rounds):
            debug_log(
                "agent.tool_round_started",
                round_index=round_index,
                pending_calls=len(pending_calls),
            )
            async for event in self._execute_tool_calls_stream(pending_calls):
                yield event

            from meteora.toolbox.builtin_tools import get_vision_usage, reset_vision_usage
            vision_usage = get_vision_usage()
            if vision_usage:
                self.tracker.add_vision(vision_usage)
                reset_vision_usage()

            if self._direct_response:
                response = self._direct_response
                self.messages.append(Message(role="assistant", content=response))
                yield StreamEvent(type="text", content=response)
                debug_log(
                    "agent.tool_loop_direct_response",
                    round_index=round_index,
                    response_length=len(response),
                )
                return

            yield StreamEvent(type="status", content="正在整理结果...")
            response = ""
            pending_calls = []
            text_sanitizer = _StreamingTextSanitizer()
            async for event in self.llm.chat_with_tools_stream(self.messages, tools):
                if event.type == "text":
                    content = text_sanitizer.push(event.content)
                    if content:
                        response += content
                        debug_log("agent.stream_text_after_tool", content_length=len(content))
                        yield StreamEvent(type="text", content=content)
                elif event.type == "tool_call":
                    pending_calls.append(event.tool_call)
                    debug_log("agent.stream_followup_tool_call", tool_name=event.tool_call.name)
                elif event.type == "done":
                    self.tracker.add_llm(event.usage)

            tail = text_sanitizer.flush()
            if tail:
                response += tail
                debug_log(
                    "agent.stream_text_after_tool",
                    content_length=len(tail),
                    flushed=True,
                )
                yield StreamEvent(type="text", content=tail)

            if not pending_calls:
                injected = _inject_refs_if_missing(response, self._ref_urls)
                if injected != response:
                    extra = injected[len(response):]
                    yield StreamEvent(type="text", content=extra)
                    response = injected
                self.messages.append(Message(role="assistant", content=response))
                debug_log(
                    "agent.tool_loop_completed",
                    round_index=round_index,
                    response_length=len(response),
                )
                return

            if round_index == self.max_tool_rounds - 1:
                stop_message = (
                    f"处理步骤已达上限（{self.max_tool_rounds} 轮），已暂停。\n\n"
                    f"需要继续吗？可以输入 `/set max_tool_rounds 50` 把上限调高，然后我继续处理。"
                )
                self.messages.append(Message(role="assistant", content=stop_message))
                yield StreamEvent(type="text", content=stop_message)
                debug_log("agent.tool_loop_limit_reached", max_tool_rounds=self.max_tool_rounds)
                return

            self.messages.append(
                Message(role="assistant", content=response or "", tool_calls=pending_calls)
            )
            debug_log("agent.tool_loop_continues", pending_calls=len(pending_calls))
            yield StreamEvent(type="status", content="继续执行...")

    async def _execute_tool_calls_stream(
        self,
        tool_calls: list[ToolCall],
    ) -> AsyncGenerator[StreamEvent, None]:
        # Group tool calls by name so same-tool batch gets confirmed once
        groups: dict[str, list[ToolCall]] = {}
        for tc in tool_calls:
            groups.setdefault(tc.name, []).append(tc)

        for tool_name, calls in groups.items():
            debug_log("agent.tool_group_started", tool_name=tool_name, calls=len(calls))
            spec = self.registry.get(tool_name)
            if spec is None:
                err = f"当前操作不可用：{_sanitize_user_facing_text(tool_name)}"
                debug_log("agent.tool_missing", tool_name=tool_name)
                for tc in calls:
                    yield StreamEvent(type="status", content=err)
                    self.messages.append(Message(role="tool", content=err, tool_call_id=tc.id))
                continue

            reason = block_reason(tool_name, self.config.mode)
            if reason is not None:
                debug_log("agent.tool_blocked", tool_name=tool_name, mode=self.config.mode)
                for tc in calls:
                    yield StreamEvent(type="status", content=reason)
                    self.messages.append(Message(role="tool", content=reason, tool_call_id=tc.id))
                continue

            if spec.requires_confirmation and tool_name not in self.always_allow:
                all_args = [self._parse_tool_args(tc) for tc in calls]
                _needs_confirm = any(
                    _tool_call_needs_confirmation(tool_name, args)
                    for args in all_args
                )
                if _needs_confirm:
                    debug_log(
                        "agent.tool_confirmation_requested",
                        tool_name=tool_name,
                        calls=len(calls),
                    )
                    self.confirm_future = asyncio.get_running_loop().create_future()
                    if len(calls) == 1:
                        yield StreamEvent(
                            type="confirm",
                            content=json.dumps(
                                {"tool": tool_name, "args": all_args[0]},
                                ensure_ascii=False,
                            ),
                        )
                    else:
                        yield StreamEvent(
                            type="confirm",
                            content=json.dumps(
                                {"tool": tool_name, "batch_args": all_args},
                                ensure_ascii=False,
                            ),
                        )
                    try:
                        choice = await self.confirm_future
                    finally:
                        self.confirm_future = None
                    if choice == "always":
                        self.always_allow.add(tool_name)
                    debug_log("agent.tool_confirmation_answered", tool_name=tool_name, choice=choice)
                    if choice == "deny":
                        for tc in calls:
                            if tool_name == "propose_execution":
                                deny_msg = json.dumps(
                                    {"approved": False, "message": "用户选择暂不执行，继续留在规划模式完善方案。"},
                                    ensure_ascii=False,
                                )
                            else:
                                deny_msg = json.dumps({"error": "用户拒绝执行此操作"}, ensure_ascii=False)
                            self.messages.append(
                                Message(role="tool", content=deny_msg, tool_call_id=tc.id)
                            )
                        continue

            for tc in calls:
                async for event in self._execute_tool_stream_body(tc):
                    yield event

    async def _execute_one_tool_stream(
        self,
        tool_call: ToolCall,
    ) -> AsyncGenerator[StreamEvent, None]:
        async for event in self._execute_tool_calls_stream([tool_call]):
            yield event

    async def _execute_tool_stream_body(
        self,
        tc: ToolCall,
    ) -> AsyncGenerator[StreamEvent, None]:
        spec = self.registry.get(tc.name)
        parsed_args = self._parse_tool_args(tc)
        debug_log(
            "agent.tool_started",
            tool_name=tc.name,
            arg_keys=sorted(parsed_args.keys()),
        )
        yield StreamEvent(
            type="status",
            content=_tool_progress_message(tc.name, "start", parsed_args),
        )

        queue: asyncio.Queue[str] = asyncio.Queue()
        reporter = ProgressReporter(
            asyncio.get_running_loop(),
            queue,
            self._cancel_event,
        )
        with use_progress_reporter(reporter):
            task = asyncio.create_task(self.runtime.execute(spec.function, parsed_args))
            while not task.done():
                if self._cancel_event is not None and self._cancel_event.is_set():
                    task.cancel()
                    debug_log("agent.tool_cancelled", tool_name=tc.name)
                    yield StreamEvent(type="status", content="已请求中断当前任务")
                    raise asyncio.CancelledError
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=1)
                    debug_log(
                        "agent.tool_progress",
                        tool_name=tc.name,
                        message_length=len(message),
                    )
                    yield StreamEvent(type="status", content=_sanitize_progress_text(message))
                except asyncio.TimeoutError:
                    pass

            exec_result = await task

        while not queue.empty():
            message = queue.get_nowait()
            debug_log(
                "agent.tool_progress",
                tool_name=tc.name,
                message_length=len(message),
                drained=True,
            )
            yield StreamEvent(type="status", content=_sanitize_progress_text(message))

        if exec_result.success and not _tool_result_has_error_status(exec_result.result):
            yield StreamEvent(
                type="status",
                content=_tool_progress_message(tc.name, "done", parsed_args),
            )
            result_str = json.dumps(exec_result.result, ensure_ascii=False, default=str)
            self._apply_runtime_config_update(tc.name, exec_result.result)
            direct_response = _direct_tool_response(tc.name, exec_result.result)
            if direct_response:
                self._direct_response = direct_response
            for url in _collect_ref_urls(exec_result.result):
                if url not in self._ref_urls:
                    self._ref_urls.append(url)
            debug_log("agent.tool_finished", tool_name=tc.name, success=True)
        elif exec_result.success:
            result_str = json.dumps(exec_result.result, ensure_ascii=False, default=str)
            error_message = ""
            if isinstance(exec_result.result, dict):
                error_message = str(exec_result.result.get("message") or exec_result.result.get("error") or "")
            if error_message.startswith(_tool_progress_message(tc.name, "error", parsed_args)):
                content = _sanitize_progress_text(error_message)
            else:
                content = (
                    f"{_tool_progress_message(tc.name, 'error', parsed_args)}："
                    f"{_sanitize_progress_text(error_message)}"
                )
            yield StreamEvent(
                type="status",
                content=content,
            )
            debug_log(
                "agent.tool_finished",
                tool_name=tc.name,
                success=False,
                status_error=True,
            )
        else:
            error_text = _sanitize_progress_text(str(exec_result.error))
            yield StreamEvent(
                type="status",
                content=f"{_tool_progress_message(tc.name, 'error', parsed_args)}：{error_text}",
            )
            result_str = json.dumps({"error": exec_result.error}, ensure_ascii=False)
            debug_log(
                "agent.tool_finished",
                tool_name=tc.name,
                success=False,
                error=exec_result.error,
            )

        self.messages.append(Message(role="tool", content=result_str, tool_call_id=tc.id))

    def _apply_runtime_config_update(self, tool_name: str, result: object) -> None:
        if tool_name not in {"configure_llm_provider", "clear_llm_config"}:
            return
        if not isinstance(result, dict):
            return
        if not result.get("llm_config_updated"):
            return

        provider = str(result.get("provider") or self.config.llm.provider)
        model = str(result.get("model") or self.config.llm.model)
        base_url = str(result.get("base_url") or "")

        self.config.llm.switch_provider(provider)
        self.config.llm.model = model
        self.config.llm.base_url = base_url
        self.llm.config.provider = provider
        self.llm.config.model = model
        self.llm.config.base_url = base_url
        self.llm.config.api_key = self.config.llm.active_api_key()

        # The raw API key is intentionally not included in the tool result sent to
        # the LLM. Reload it from the just-saved project config instead.
        try:
            from meteora.toolbox.builtin_tools import _find_config_path

            config_path = _find_config_path()
            if config_path.exists():
                fresh = MeteoraConfig.load(config_path)
                self.config.llm.providers = fresh.llm.providers
                self.llm.config.api_key = fresh.llm.active_api_key()
                self.config.llm.reasoning_effort = fresh.llm.reasoning_effort
                self.llm.config.reasoning_effort = fresh.llm.reasoning_effort
        except Exception as e:
            debug_log("agent.llm_config_reload_failed", error=repr(e))

        preset = get_provider_preset(provider)
        debug_log(
            "agent.llm_config_updated",
            provider=provider,
            provider_name=preset.name if preset else provider,
            model=model,
            base_url=base_url,
        )

    @staticmethod
    def _parse_tool_args(tc: ToolCall) -> dict:
        if isinstance(tc.arguments, str):
            try:
                return json.loads(tc.arguments)
            except json.JSONDecodeError:
                return {}
        return tc.arguments

    async def _ask_confirmation_simple(self, tool_name: str, args: dict) -> str:
        print(f"\n⚠️  危险操作: {tool_name}")
        args_summary = json.dumps(args, ensure_ascii=False, default=str)
        if len(args_summary) > 200:
            args_summary = args_summary[:200] + "..."
        print(f"   参数: {args_summary}")
        print("   此操作不可撤销，是否允许？")
        print("   [y] 允许  [a] 一直允许  [n] 拒绝")

    async def _ask_confirmation_simple_batch(self, tool_name: str, all_args: list[dict]) -> str:
        print(f"\n⚠️  危险操作: {tool_name}（共 {len(all_args)} 次调用）")
        for i, args in enumerate(all_args, 1):
            summary = json.dumps(args, ensure_ascii=False, default=str)
            if len(summary) > 200:
                summary = summary[:200] + "..."
            print(f"   #{i}: {summary}")
        print("   此操作不可撤销，是否允许所有？")
        print("   [y] 允许全部  [a] 一直允许  [n] 拒绝全部")

        loop = asyncio.get_running_loop()
        while True:
            answer = await loop.run_in_executor(None, input, "   选择: ")
            answer = answer.strip().lower()
            if answer in ("y", "yes"):
                return "allow"
            if answer in ("a", "always"):
                return "always"
            if answer in ("n", "no", ""):
                return "deny"
            print("   请输入 y/a/n")


def _sanitize_tool_message_sequence(messages: list[Message]) -> list[Message]:
    sanitized: list[Message] = []
    index = 0
    while index < len(messages):
        msg = messages[index]
        if msg.role == "tool":
            index += 1
            continue

        if msg.role == "assistant" and msg.tool_calls:
            pending_tool_ids = {tc.id for tc in msg.tool_calls}
            tool_messages: list[Message] = []
            seen_tool_ids: set[str] = set()
            cursor = index + 1
            while cursor < len(messages) and messages[cursor].role == "tool":
                tool_msg = messages[cursor]
                tool_call_id = tool_msg.tool_call_id or ""
                if tool_call_id in pending_tool_ids and tool_call_id not in seen_tool_ids:
                    tool_messages.append(tool_msg)
                    seen_tool_ids.add(tool_call_id)
                cursor += 1
            if seen_tool_ids == pending_tool_ids:
                sanitized.append(msg)
                sanitized.extend(tool_messages)
            elif msg.content.strip():
                sanitized.append(Message(role="assistant", content=msg.content))
            index = cursor
            continue

        sanitized.append(msg)
        index += 1
    return sanitized


def _tool_start_message(tool_name: str) -> str:
    messages = {
        "search_cds_variables": "正在查询数据变量信息...",
        "search_gfs_variables": "正在查询 GFS 可用要素...",
        "lookup_gfs_parameter": "正在查阅 GFS 要素定义...",
        "check_gfs_availability": "正在检查 GFS 可用时次...",
        "inspect_gfs_inventory": "正在查看 GFS 文件库存...",
        "lookup_ecmwf_parameter": "正在查阅 ECMWF 参数定义...",
        "describe_cds_dataset": "正在读取数据集说明...",
        "download_era5": "正在准备并提交数据下载任务...",
        "subset_netcdf": "正在裁剪 NetCDF 文件...",
        "download_gfs": "正在准备 GFS 预报数据下载...",
        "list_downloads": "正在查看下载任务记录...",
        "query_download": "正在查询下载任务状态...",
        "retry_download": "正在重试下载任务...",
        "cleanup_downloads": "正在清理下载记录...",
        "check_cds_config": "正在检查 CDS 配置...",
        "configure_cds_key": "正在保存 CDS 配置...",
        "configure_email_config": "正在保存邮箱配置...",
        "check_email_config": "正在检查邮箱配置...",
        "send_email": "正在发送邮件...",
        "clear_cds_config": "正在清除 CDS 配置...",
        "ensure_runtime_tools": "正在配置运行时命令行工具...",
        "inspect_nc": "正在检查数据文件...",
        "inspect_grib2": "正在检查 GRIB2 文件...",
        "list_files": "正在查看文件列表...",
        "list_figures": "正在查看图片列表...",
        "delete_file": "正在删除文件...",
        "search_literature": "正在检索学术文献...",
        "save_literature": "正在保存文献信息...",
        "download_literature_pdf": "正在下载文献全文...",
        "list_literature": "正在查看已保存文献...",
        "read_pdf": "正在提取 PDF 文本内容...",
        "record_instruction": "正在记录个性化指令...",
        "show_instructions": "正在查看已有指令...",
        "clear_instructions": "正在清空指令记录...",
        "write_plan_document": "正在保存计划书...",
    }
    return messages.get(tool_name, "正在处理请求...")


def _tool_done_message(tool_name: str) -> str:
    messages = {
        "search_cds_variables": "数据变量信息查询完成",
        "search_gfs_variables": "GFS 可用要素查询完成",
        "lookup_gfs_parameter": "GFS 要素定义查询完成",
        "check_gfs_availability": "GFS 可用时次检查完成",
        "inspect_gfs_inventory": "GFS 文件库存查看完成",
        "lookup_ecmwf_parameter": "ECMWF 参数定义查询完成",
        "describe_cds_dataset": "数据集说明读取完成",
        "download_era5": "数据下载处理完成",
        "subset_netcdf": "NetCDF 文件裁剪完成",
        "download_gfs": "GFS 预报数据下载完成",
        "list_downloads": "下载任务记录已读取",
        "query_download": "下载任务状态已读取",
        "retry_download": "下载任务重试完成",
        "cleanup_downloads": "下载记录清理完成",
        "check_cds_config": "CDS 配置检查完成",
        "configure_cds_key": "CDS 配置已保存",
        "configure_email_config": "邮箱配置已保存",
        "check_email_config": "邮箱配置检查完成",
        "send_email": "邮件已发送",
        "clear_cds_config": "CDS 配置已清除",
        "ensure_runtime_tools": "运行时命令行工具配置完成",
        "inspect_nc": "数据文件检查完成",
        "inspect_grib2": "GRIB2 文件检查完成",
        "list_files": "文件列表读取完成",
        "list_figures": "图片列表读取完成",
        "delete_file": "文件删除完成",
        "search_literature": "学术文献检索完成",
        "save_literature": "文献信息保存完成",
        "download_literature_pdf": "文献全文下载完成",
        "list_literature": "已保存文献列表读取完成",
        "read_pdf": "PDF 文本内容提取完成",
        "record_instruction": "个性化指令已记录",
        "show_instructions": "已有指令查看完成",
        "clear_instructions": "指令记录已清空",
        "write_plan_document": "计划书已保存",
    }
    return messages.get(tool_name, "处理完成")


def _tool_error_prefix(tool_name: str) -> str:
    messages = {
        "download_era5": "数据下载处理失败",
        "subset_netcdf": "NetCDF 文件裁剪失败",
        "download_gfs": "GFS 预报数据下载失败",
        "retry_download": "下载任务重试失败",
        "search_cds_variables": "数据变量信息查询失败",
        "search_gfs_variables": "GFS 可用要素查询失败",
        "lookup_gfs_parameter": "GFS 要素定义查询失败",
        "check_gfs_availability": "GFS 可用时次检查失败",
        "inspect_gfs_inventory": "GFS 文件库存查看失败",
        "lookup_ecmwf_parameter": "ECMWF 参数定义查询失败",
        "describe_cds_dataset": "数据集说明读取失败",
        "list_downloads": "下载任务记录读取失败",
        "query_download": "下载任务状态查询失败",
        "cleanup_downloads": "下载记录清理失败",
        "check_cds_config": "CDS 配置检查失败",
        "configure_cds_key": "CDS 配置保存失败",
        "configure_email_config": "邮箱配置保存失败",
        "check_email_config": "邮箱配置检查失败",
        "send_email": "邮件发送失败",
        "clear_cds_config": "CDS 配置清除失败",
        "ensure_runtime_tools": "运行时命令行工具配置失败",
        "inspect_nc": "数据文件检查失败",
        "inspect_grib2": "GRIB2 文件检查失败",
        "list_files": "文件列表读取失败",
        "list_figures": "图片列表读取失败",
        "delete_file": "文件删除失败",
        "search_literature": "学术文献检索失败",
        "save_literature": "文献信息保存失败",
        "download_literature_pdf": "文献全文下载失败",
        "list_literature": "已保存文献列表读取失败",
        "read_pdf": "PDF 文本内容提取失败",
        "record_instruction": "记录指令失败",
        "show_instructions": "查看指令失败",
        "clear_instructions": "清空指令失败",
        "write_plan_document": "计划书保存失败",
    }
    return messages.get(tool_name, "处理失败")
