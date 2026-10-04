"""Agent loop — the core orchestration of Aero.

Supports both non-streaming and streaming modes.
"""

import asyncio
import json
import re
import shlex
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, urlunparse

import structlog

from aero.agent.llm_client import LLMClient, LLMConfig, StreamEvent
from aero.agent.progress import ProgressReporter, use_progress_reporter
from aero.agent.runtime import Runtime
from aero.agent.skills import SelectedSkill, SkillSelector, render_skill_context
from aero.agent.system_prompt import build_system_prompt
from aero.application.turn_context import use_primary_model
from aero.core.config import AeroConfig
from aero.core.debug_log import debug_log
from aero.core.llm_providers import get_provider_preset
from aero.core.types import Message, ToolCall
from aero.data.modes import block_reason
from aero.data.pricing import TokenTracker, use_token_tracker
from aero.toolbox.registry import get_registry

logger = structlog.get_logger()

_MAX_VISION_ANALYSES_PER_TURN = 2

# A short follow-up such as “改成 3×3” has no domain keywords by itself, even
# though its meaning depends entirely on the previous plotting request.  Keep
# skill selection grounded in recent *user* intent for those referential turns.
_SKILL_FOLLOW_UP_MARKERS = (
    "改画",
    "改成",
    "重新画",
    "再画",
    "重画",
    "上一张",
    "刚才",
    "这张图",
    "这个图",
    "上述图",
    "多子图",
    "子图",
    "3x3",
    "2x2",
    "3×3",
    "2×2",
    "subplot",
    "panel",
    "same plot",
    "previous plot",
    "redraw",
)


def _image_fingerprint(path: Path) -> tuple[int, int] | None:
    """Return a cheap source-image version marker without reading its contents."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


_DESTRUCTIVE_SHELL_PREFIXES = (
    "rm ",
    "cp ",
    "mv ",
    "dd ",
    "mkfs",
    "chmod ",
    "chown ",
    "kill ",
    "pkill ",
    "shutdown",
    "reboot",
    "halt ",
    "poweroff",
    "init ",
    "systemctl stop",
    "systemctl disable",
    "systemctl mask",
    "docker rm",
    "docker kill",
    "pip uninstall",
    "pip install ",
    "npm uninstall",
    "npm install ",
    "gem uninstall",
    "gem install ",
    "cargo uninstall",
    "cargo install",
    "apt ",
    "brew ",
    "yum ",
    "dnf ",
    "pacman ",
    "zypper ",
    "port install",
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
    "download_cams",
    "subset_netcdf",
    "download_gfs",
    "download_ifs",
    "download_gefs",
    "scan_local_files",
    "ensure_runtime_tools",
    "run_shell",
    "write_file",
    "edit_file",
    "delete_file",
    "write_plan_document",
}


def _destructive_memo_tools_for_text(text: str) -> set[str]:
    """Expose destructive memo tools only for an explicit current-turn request."""
    lowered = str(text or "").lower()
    if not any(marker in lowered for marker in ("备忘录", "备忘", "memo")):
        return set()
    clear_requested = any(
        marker in lowered
        for marker in ("清空", "清理全部", "删除全部", "删除所有", "clear all", "clear memos")
    )
    if clear_requested:
        return {"clear_memos"}
    delete_requested = any(
        marker in lowered for marker in ("删除", "删掉", "移除", "delete", "remove")
    )
    return {"delete_memo"} if delete_requested else set()


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

    from aero.toolbox.paths import find_workspace_dir

    project_dir = find_workspace_dir().resolve()
    base = Path(workdir).expanduser()
    if not base.is_absolute():
        base = project_dir / base
    if not base.is_dir():
        base = project_dir
    return all(
        (
            Path(target).expanduser() if Path(target).expanduser().is_absolute() else base / target
        ).is_dir()
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
        from aero.toolbox.runtime_manager import get_runtime_tool_manager

        manager = get_runtime_tool_manager()
        requested = [str(tool).strip() for tool in args.get("tools", []) if str(tool).strip()]
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
    if tool_name == "scan_local_files":
        return bool(args.get("confirm"))
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
        "Content Exists Risk" in msg or "content_filter" in msg.lower() or "safety" in msg.lower()
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
    "download_cams": "下载 CAMS 数据",
    "subset_netcdf": "裁剪 NetCDF 文件",
    "inspect_nc": "查看文件详情",
    "inspect_grib2": "查看 GRIB2 文件详情",
    "scan_local_files": "扫描本地科研数据",
    "search_cds_variables": "查询可用变量",
    "search_cams_variables": "查询 CAMS 变量",
    "get_cams_latest_forecast_cycle": "查询 CAMS 最新可用起报",
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
    "check_ads_config": "检查 ADS 配置",
    "configure_ads_key": "保存 ADS 凭证",
    "configure_cds_key": "保存 CDS 凭证",
    "configure_earthdata_token": "保存 Earthdata 凭证",
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
    "prepare_image_for_vision": "压缩图片以供视觉模型分析",
    "configure_vision_model": "配置视觉模型",
    "search_literature": "检索学术文献",
    "search_web": "搜索互联网",
    "save_literature": "保存文献信息",
    "download_literature_pdf": "下载文献全文",
    "list_literature": "查看文献列表",
    "read_pdf": "提取 PDF 文本",
    "preview_image": "打开图片预览",
    "preview_pdf": "打开 PDF",
    "get_max_tool_rounds": "查询工具轮次上限",
    "set_max_tool_rounds": "设置工具轮次上限",
    "launch_sub_agent": "转交后台任务",
    "query_sub_agents": "查询后台任务状态",
    "cancel_sub_agent": "取消后台任务",
    "record_instruction": "记录个性化指令",
    "show_instructions": "查看已有指令",
    "clear_instructions": "清空指令记录",
    "record_memo": "加入研究备忘录",
    "show_memos": "查看研究备忘录",
    "update_memo": "更新研究备忘录",
    "delete_memo": "删除研究备忘录",
    "clear_memos": "清理研究备忘录",
    "initialize_paper_versioning": "启用论文版本管理",
    "paper_version_status": "查看论文版本状态",
    "save_paper_version": "保存论文版本",
    "list_paper_versions": "查看论文版本历史",
    "diff_paper_version": "比较论文版本",
    "restore_paper_version": "恢复论文版本",
    "export_paper": "导出论文",
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
    "scan_local_files": ("正在扫描本地科研数据", "本地数据扫描完成", "本地数据扫描失败"),
    "search_cds_variables": ("正在查询可用变量", "可用变量查询完成", "查询可用变量失败"),
    "search_cams_variables": (
        "正在查询 CAMS 可用变量",
        "CAMS 可用变量查询完成",
        "查询 CAMS 可用变量失败",
    ),
    "get_cams_latest_forecast_cycle": (
        "正在查询 CAMS 最新可用起报",
        "CAMS 最新可用起报查询完成",
        "查询 CAMS 最新可用起报失败",
    ),
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
    "download_cams": ("准备下载 CAMS 数据", "CAMS 数据下载完成", "CAMS 数据下载失败"),
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
    "check_ads_config": ("正在检查 ADS 配置", "ADS 配置检查完成", "检查 ADS 配置失败"),
    "configure_ads_key": ("正在保存 ADS 凭证", "ADS 凭证已保存", "保存 ADS 凭证失败"),
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
    "check_earthdata_config": (
        "正在检查 Earthdata 配置",
        "Earthdata 配置检查完成",
        "检查 Earthdata 配置失败",
    ),
    "configure_earthdata_token": (
        "正在保存 Earthdata 凭证",
        "Earthdata 凭证已保存",
        "保存 Earthdata 凭证失败",
    ),
    "describe_cds_dataset": ("正在查看数据集信息", "数据集信息查看完成", "查看数据集信息失败"),
    "query_download": ("正在查询下载状态", "下载状态查询完成", "查询下载状态失败"),
    "check_vision_model_config": (
        "正在检查视觉模型配置",
        "视觉模型配置检查完成",
        "检查视觉模型配置失败",
    ),
    "analyze_image": ("正在调用视觉模型分析图片", "图片分析完成", "图片分析失败"),
    "prepare_image_for_vision": ("正在压缩图片", "图片压缩完成", "图片压缩失败"),
    "configure_vision_model": ("正在配置视觉模型", "视觉模型配置完成", "视觉模型配置失败"),
    "search_literature": ("正在检索学术文献", "学术文献检索完成", "学术文献检索失败"),
    "search_web": (
        "正在调用外部网页搜索服务",
        "外部网页搜索完成",
        "外部网页搜索失败",
    ),
    "save_literature": ("正在保存文献信息", "文献信息保存完成", "文献信息保存失败"),
    "download_literature_pdf": ("正在下载文献全文", "文献全文下载完成", "文献全文下载失败"),
    "list_literature": ("正在查看文献列表", "文献列表查看完成", "查看文献列表失败"),
    "read_pdf": ("正在提取 PDF 文本内容", "PDF 文本提取完成", "PDF 文本提取失败"),
    "preview_image": ("正在打开图片", "图片预览已打开", "打开图片失败"),
    "preview_pdf": ("正在打开 PDF", "PDF 已打开", "打开 PDF 失败"),
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
    "record_memo": ("正在准备研究备忘录", "研究备忘录已记录", "记录备忘录失败"),
    "show_memos": ("正在查看研究备忘录", "研究备忘录查看完成", "查看备忘录失败"),
    "update_memo": ("正在准备更新研究备忘录", "研究备忘录已更新", "更新备忘录失败"),
    "delete_memo": ("正在删除研究备忘录", "研究备忘录已删除", "删除备忘录失败"),
    "clear_memos": ("正在清理研究备忘录", "研究备忘录已清理", "清理备忘录失败"),
    "initialize_paper_versioning": (
        "正在启用论文版本管理",
        "论文版本管理已启用",
        "启用论文版本管理失败",
    ),
    "paper_version_status": (
        "正在查看论文版本状态",
        "论文版本状态查看完成",
        "查看论文版本状态失败",
    ),
    "save_paper_version": ("正在保存论文版本", "论文版本已保存", "保存论文版本失败"),
    "list_paper_versions": (
        "正在查看论文版本历史",
        "论文版本历史查看完成",
        "查看论文版本历史失败",
    ),
    "diff_paper_version": ("正在比较论文版本", "论文版本比较完成", "比较论文版本失败"),
    "restore_paper_version": ("正在恢复论文版本", "论文版本已恢复", "恢复论文版本失败"),
    "export_paper": ("正在转换论文格式", "论文文件已生成", "论文格式转换失败"),
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
            from aero.toolbox.tools.runtime import _normalize_shell_context, _redact_shell_command

            raw_command, _workdir, _correction = _normalize_shell_context(
                raw_command,
                str(args.get("workdir") or "."),
            )
            raw_command = _redact_shell_command(raw_command)
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
    return isinstance(result, dict) and (
        result.get("is_error") or result.get("status") in {"error", "failed", "unknown", "blocked"}
    )


def _tool_error_progress_message(tool_name: str, args: dict | None, result: object) -> str:
    """Describe expected validation feedback without calling it a command failure."""
    if (
        tool_name == "run_shell"
        and isinstance(result, dict)
        and result.get("scientific_plot_validation_failed")
    ):
        return "绘图脚本检查未通过"
    return _tool_progress_message(tool_name, "error", args)


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
    text = re.sub(
        r"下载时通过\s+下载 CAMS 数据\s*(?:工具|函数)?，\s*"
        r"指定\s+`?dataset_id`?\s+为\s+`?cams-global-reanalysis-eac4`?\s*，",
        "下载时选择 CAMS EAC4 再分析数据集，",
        text,
    )
    text = re.sub(
        r"下载时通过\s+下载 CAMS 数据\s*(?:工具|函数)?，\s*"
        r"指定\s+`?dataset_id`?\s+为\s+"
        r"`?cams-global-atmospheric-composition-forecasts`?\s*，",
        "下载时选择 CAMS 全球大气成分预报数据集，",
        text,
    )
    text = re.sub(r"通过\s+下载 CAMS 数据\s*(?:工具|函数)?", "选择 CAMS 数据下载", text)
    text = re.sub(r"通过\s+下载数据\s*(?:工具|函数)?", "选择数据下载", text)
    text = re.sub(r"选择 CAMS 数据下载\s*就能下载", "我可以直接帮你下载", text)
    text = re.sub(r"选择数据下载\s*就能下载", "我可以直接帮你下载", text)
    text = re.sub(r"指定\s+`?dataset_id`?\s+为", "选择数据集", text)
    text = re.sub(r"选择数据集\s+([A-Za-z0-9_.-]+)", r"选择数据集 \1", text)
    text = text.replace("下载时选择 CAMS 数据下载，选择数据集", "下载时选择")
    text = text.replace("下载时选择数据下载，选择数据集", "下载时选择")
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
        ready = self._pending[: -self._hold_chars]
        self._pending = self._pending[-self._hold_chars :]
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
        url = _encode_reference_url(u)
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return _prune_generic_reference_urls(deduped)


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def _inject_refs_if_missing(text: str, ref_urls: list[str]) -> str:
    ref_urls = _prune_generic_reference_urls([_encode_reference_url(url) for url in ref_urls])
    if not ref_urls:
        return _format_reference_links(text)
    if "参考资料" in text or "References" in text.lower():
        return _format_reference_links(text)
    lines = ["\n\n参考资料"]
    for index, url in enumerate(ref_urls, start=1):
        label = _reference_label(url)
        lines.append(f"{index}. [{label}]({url})")
    return _format_reference_links(text + "\n".join(lines))


def _format_reference_links(text: str) -> str:
    """Render reference links as clickable Markdown text links."""
    lines = text.splitlines()
    in_refs = False
    formatted: list[str] = []
    link_re = re.compile(
        r"^(?P<prefix>\s*(?:(?P<num>\d+)\.\s*|[-*]\s*)?)"
        r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)]+)\)\s*$"
    )
    plain_re = re.compile(
        r"^(?P<prefix>\s*(?:(?P<num>\d+)\.\s*|[-*]\s*)?)"
        r"(?P<label>.*?\S)\s+(?P<url>https?://\S+)\s*$"
    )
    index = 1
    for line in lines:
        stripped = line.strip()
        if stripped in {"参考资料", "References"}:
            in_refs = True
            formatted.append(line)
            continue
        if in_refs and stripped.startswith("#") and stripped not in {"参考资料", "References"}:
            in_refs = False
        if in_refs:
            match = link_re.match(line)
            if match:
                label = re.sub(r"\s+#\d+$", "", match.group("label").strip())
                url = _encode_reference_url(match.group("url"))
                number = match.group("num") or str(index)
                formatted.append(f"{number}. [{label}]({url})")
                index = int(number) + 1 if number.isdigit() else index + 1
                continue
            match = plain_re.match(line)
            if match:
                label = match.group("label").strip().rstrip("：:")
                url = _encode_reference_url(match.group("url"))
                number = match.group("num") or str(index)
                formatted.append(f"{number}. [{label}]({url})")
                index = int(number) + 1 if number.isdigit() else index + 1
                continue
        formatted.append(line)
    return "\n".join(formatted)


def _encode_reference_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    path = _normalize_reference_path(parsed.netloc, unquote(parsed.path))
    path = quote(path, safe="/%")
    query = quote(unquote(parsed.query), safe="=&?/:;+,%")
    fragment = quote(unquote(parsed.fragment), safe="=&?/:;+,%")
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, query, fragment))


def _normalize_reference_path(host: str, path: str) -> str:
    if "ads.atmosphere.copernicus.eu" not in host.lower():
        return path
    if not path.lower().startswith("/datasets/"):
        return path
    dataset_text = path.split("/datasets/", 1)[1].strip("/")
    dataset_key = dataset_text.casefold().replace("_", "-").replace("%20", " ")
    if (
        "atmospheric-composition-forecasts" in dataset_key
        or "global-atmospheric-composition-forecasts" in dataset_key
        or "全球大气成分预报" in dataset_key
        or "预报数据集" in dataset_key
    ):
        return "/datasets/cams-global-atmospheric-composition-forecasts"
    if (
        "global-reanalysis-eac4" in dataset_key
        or "reanalysis-eac4" in dataset_key
        or "eac4" in dataset_key
        or "全球再分析" in dataset_key
        or "再分析" in dataset_key
    ):
        return "/datasets/cams-global-reanalysis-eac4"
    return path


def _prune_generic_reference_urls(urls: list[str]) -> list[str]:
    """Drop generic landing pages when a specific page from the same source exists."""
    parsed_urls = [(url, urlparse(url)) for url in urls]
    specific_hosts = {
        parsed.netloc.lower()
        for _url, parsed in parsed_urls
        if parsed.path and parsed.path not in {"", "/"}
    }
    pruned: list[str] = []
    seen: set[str] = set()
    for url, parsed in parsed_urls:
        host = parsed.netloc.lower()
        is_generic = parsed.path in {"", "/"}
        if is_generic and host in specific_hosts:
            continue
        if url not in seen:
            seen.add(url)
            pruned.append(url)
    return pruned


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
    if "ads.atmosphere.copernicus.eu" in host and "/datasets/cams-global-reanalysis-eac4" in path:
        return "CAMS EAC4 数据集下载页"
    if (
        "ads.atmosphere.copernicus.eu" in host
        and "/datasets/cams-global-atmospheric-composition-forecasts" in path
    ):
        return "CAMS 全球大气成分预报下载页"
    if "ads.atmosphere.copernicus.eu" in host and "/datasets" in path:
        return "CAMS/ADS 数据集下载页"
    if "ads.atmosphere.copernicus.eu" in host:
        return "Copernicus ADS"
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


def _skill_selection_text(user_message: str, messages: list[Message]) -> str:
    """Add recent user intent only for an explicitly referential follow-up."""
    normalized = str(user_message or "").lower()
    if not any(marker in normalized for marker in _SKILL_FOLLOW_UP_MARKERS):
        return user_message

    recent_user_messages: list[str] = []
    for message in reversed(messages):
        if message.role != "user" or not message.content.strip():
            continue
        recent_user_messages.append(message.content)
        if len(recent_user_messages) == 3:
            break

    if not recent_user_messages:
        return user_message
    return "\n".join([*reversed(recent_user_messages), user_message])


def _active_experiment_context() -> str:
    """Describe the active experiment without exposing storage internals."""
    try:
        from aero.experiments import ExperimentManager
        from aero.toolbox.paths import find_project_dir

        manager = ExperimentManager(find_project_dir())
        experiment = manager.active()
        if experiment is None:
            return ""
        workspace = manager.workspace_path(experiment)
        return (
            f"当前实验：{experiment['name']}\n"
            f"实验 ID：{experiment['id']}\n"
            f"实验工作目录：{workspace}\n"
            "相对文件路径和命令默认以该实验目录为根。脚本写入 scripts/，"
            "图片写入 figures/，计划写入 plans/，其他中间产物写入 outputs/ 或 tmp/。\n"
            "优先围绕当前实验目标工作，不要把脚本或中间产物写回项目公共目录；"
            "只有用户明确要求共享或提升实验成果时才修改公共目录。"
        )
    except Exception as exc:
        debug_log("agent.experiment_context_failed", error=str(exc))
        return ""


def _project_memo_context() -> str:
    """Load research findings for summaries and writing tasks."""
    try:
        from aero.data.memos import render_memo_context
        from aero.toolbox.paths import find_project_dir

        return render_memo_context(find_project_dir())
    except Exception as exc:
        debug_log("agent.memo_context_failed", error=str(exc))
        return ""


class AgentLoop:
    def __init__(self, config: AeroConfig):
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
        from aero.agent.search_budget import SearchBudget

        self.search_budget = SearchBudget()
        self.runtime.search_budget = self.search_budget
        self.registry = get_registry()
        self.skill_selector = SkillSelector()

        from aero.data.instructions import load_instructions

        instructions = load_instructions(project_dir=self.config.output.data_dir)
        system = build_system_prompt(
            config,
            config.language,
            instructions_context=instructions,
            experiment_context=_active_experiment_context(),
            memo_context=_project_memo_context(),
        )
        self.messages: list[Message] = [
            Message(role="system", content=system),
        ]
        self._cancel_event: asyncio.Event | None = None
        self.max_tool_rounds = config.max_tool_rounds
        self.always_allow: set[str] = set()
        self.confirm_future: asyncio.Future[str] | None = None
        self._ref_urls: list[str] = []
        self._direct_response: str | None = None
        self._current_user_message = ""
        self._vision_analyses_this_turn = 0
        self._successful_vision_images: dict[str, tuple[int, int] | None] = {}
        self._official_mcp = None
        self._official_mcp_tools: list[dict] = []
        self._official_mcp_tool_names: set[str] = set()
        self.tracker = TokenTracker()

    def reset_system_prompt(self, language: str) -> None:
        self.config.language = language
        from aero.data.instructions import load_instructions

        instructions = load_instructions(project_dir=self.config.output.data_dir)
        system = build_system_prompt(
            self.config,
            language,
            instructions_context=instructions,
            experiment_context=_active_experiment_context(),
            memo_context=_project_memo_context(),
        )
        system += (
            f"\nThis turn allows at most {self.search_budget.limit} distinct web searches. "
            "The client enforces this limit; use existing evidence when exhausted."
        )
        self.messages[0] = Message(role="system", content=system)

    def _refresh_system_prompt_for_turn(self, user_message: str) -> None:
        selection_text = _skill_selection_text(user_message, self.messages)
        selected = self.skill_selector.select(selection_text)
        _ensure_cnmaps_when_sciplot_active(selected, skill_selector=self.skill_selector)
        skill_context = render_skill_context(selected)
        from aero.data.instructions import load_instructions

        instructions = load_instructions(project_dir=self.config.output.data_dir)
        system = build_system_prompt(
            self.config,
            self.config.language,
            skill_context,
            instructions,
            experiment_context=_active_experiment_context(),
            memo_context=_project_memo_context(),
        )
        system += (
            f"\nThis turn allows at most {self.search_budget.limit} distinct web searches. "
            "The client enforces this limit; use existing evidence when exhausted."
        )
        self.messages[0] = Message(role="system", content=system)
        if selected:
            debug_log(
                "agent.skills_selected",
                skills=[item.skill.name for item in selected],
            )

    def _effective_vision_identity(self) -> tuple[str, str]:
        from aero.core.config import resolved_vision_config

        vision = resolved_vision_config(self.config) or self.config.vision
        return vision.provider, vision.model

    def _start_search_turn(self, prompt: str) -> None:
        from aero.agent.search_budget import RESEARCH, SearchBudget

        if len(self.messages) <= 1:
            self.runtime.read_files.clear()
        limit = (
            self.config.search_research_budget
            if RESEARCH.search(prompt)
            else self.config.search_simple_budget
        )
        self.search_budget = SearchBudget(limit)
        self.runtime.search_budget = self.search_budget

    def _remaining_tools(self, tools: list[dict]) -> list[dict]:
        from aero.agent.search_budget import SEARCH_TOOLS

        return [
            tool
            for tool in tools
            if self.search_budget.remaining
            or tool.get("function", {}).get("name") not in SEARCH_TOOLS
        ]

    def _allowed_tools(self) -> list[dict]:
        from aero.data.modes import filter_tool_functions

        tools = filter_tool_functions(self.registry.list_functions(), self.config.mode)
        allowed_destructive_memo_tools = _destructive_memo_tools_for_text(
            self._current_user_message
        )
        official_managed_tools = (
            {"search_web", "check_web_search_status"}
            if self.config.llm.provider == "official"
            else set()
        )
        return [
            tool
            for tool in tools
            if tool.get("function", {}).get("name")
            not in ({"delete_memo", "clear_memos"} - allowed_destructive_memo_tools)
            and tool.get("function", {}).get("name") not in official_managed_tools
            and not (
                tool.get("function", {}).get("name") == "search_web"
                and not self.config.web_search.enabled
            )
        ]

    def _uses_bailian_native_web_search(self) -> bool:
        """Native model-side search is intentionally disabled."""
        return False

    def _external_web_search_block_reason(self, tool_name: str) -> str | None:
        """Keep official Relay search separate from client-managed search tools."""
        if (
            self.config.llm.provider == "official"
            and tool_name
            in {
                "search_web",
                "check_web_search_status",
            }
            and tool_name not in self._official_mcp_tool_names
        ):
            return (
                "当前使用 Aerolytica 官方模型，但本轮官方 MCP 未提供该工具；"
                "客户端不会改用外部网页搜索服务。"
            )
        return None

    async def _tools_for_turn(self) -> list[dict]:
        tools = self._allowed_tools()
        self._official_mcp_tools = []
        self._official_mcp_tool_names = set()
        if self.config.llm.provider != "official":
            return tools
        if self._official_mcp is None:
            from aero.core.official_mcp import OfficialMcpClient

            self._official_mcp = OfficialMcpClient()
        try:
            remote_tools = await self._official_mcp.list_tools()
        except Exception as exc:
            debug_log("agent.official_mcp_prepare_failed", error=repr(exc))
            self.messages[0].content += (
                "\n\n本轮 Aerolytica 官方 MCP 不可用。"
                "不要把模型记忆描述成实时检索结果，并向用户明确说明无法完成联网核验。"
            )
            return tools
        self._official_mcp_tools = remote_tools
        self._official_mcp_tool_names = {
            str(tool.get("function", {}).get("name") or "")
            for tool in remote_tools
            if tool.get("function", {}).get("name")
        }
        debug_log(
            "agent.official_mcp_prepared",
            decision="model",
            tools=sorted(self._official_mcp_tool_names),
        )
        return tools + remote_tools

    async def close(self):
        await self.llm.close()
        if self._official_mcp is not None:
            await self._official_mcp.close()

    def _record_llm_usage(self, usage: dict | None) -> None:
        self.tracker.add_llm(
            usage,
            self.config.llm.model,
            provider=self.config.llm.provider,
            occurred_at=self.llm.last_request_started_at,
        )

    async def _prepare_account_turn(self) -> None:
        from aero.application.turn_context import current_turn_id
        from aero.core.agent_preferences import begin_agent_question

        turn_id = current_turn_id() or uuid.uuid4().hex
        if getattr(self, "_prepared_turn_id", "") == turn_id:
            return
        self.llm.relay_turn_id = turn_id
        self.llm.relay_purpose = "main"
        self._main_rounds = 0
        self._account_round_limit = None
        self._account_preferences = None
        if self.config.llm.provider == "official":
            from aero.application.remote_command import current_command
            from aero.core.agent_preferences import AgentPreferences

            command = current_command()
            snapshot = command.get("preferences")
            if "max_rounds" in command:
                snapshot = {
                    "max_rounds": command["max_rounds"],
                    "version": command.get("preference_version", 1),
                }
            if snapshot is not None:
                if not isinstance(snapshot, dict):
                    raise ValueError("invalid_command_preferences")
                limit = snapshot.get("max_rounds", 999)
                if limit is not None and (type(limit) is not int or limit < 1):
                    raise ValueError("invalid_command_round_limit")
                preferences = AgentPreferences(limit, snapshot.get("version", 0))
            else:
                preferences = await begin_agent_question(turn_id)
            self._account_round_limit = preferences.max_rounds
            self._account_preferences = preferences
        self._prepared_turn_id = turn_id

    def _begin_main_round(self) -> None:
        limit = getattr(self, "_account_round_limit", None)
        rounds = getattr(self, "_main_rounds", 0)
        if limit is not None and rounds >= limit:
            raise RuntimeError(f"本轮已达到账户模型轮次上限（{limit}），请在账户设置中调整。")
        self._main_rounds = rounds + 1

    async def run(self, user_message: str) -> str:
        """Process a user message and return the agent's response (non-streaming)."""
        debug_log("agent.run_started", mode="non_stream", text_length=len(user_message))
        self._ref_urls.clear()
        self._direct_response = None
        self._current_user_message = user_message
        self._start_search_turn(user_message)
        self._reset_turn_vision_guard()
        self._drop_incomplete_tool_call_tail()
        self.messages = _sanitize_tool_message_sequence(self.messages)
        self._refresh_system_prompt_for_turn(user_message)
        self.messages.append(Message(role="user", content=user_message))
        await self._prepare_account_turn()
        self.messages[-1].message_id = self.llm.relay_turn_id
        tools = await self._tools_for_turn()

        try:
            self._begin_main_round()
            text, tool_calls = await self.llm.chat_with_tools(self.messages, tools)
            self._record_llm_references()
            self._record_llm_usage(self.llm.last_usage)
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

            visible_text = _inject_refs_if_missing(_sanitize_user_facing_text(text), self._ref_urls)
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

    async def run_stream(self, user_message: str) -> AsyncGenerator[StreamEvent, None]:
        """Process a user message with streaming output."""
        from aero.application.request_timing import start_execution_timer

        start_execution_timer()
        debug_log("agent.run_started", mode="stream", text_length=len(user_message))
        self._cancel_event = asyncio.Event()
        self._ref_urls.clear()
        self._direct_response = None
        self._current_user_message = user_message
        self._start_search_turn(user_message)
        self._reset_turn_vision_guard()
        self._drop_incomplete_tool_call_tail()
        self.messages = _sanitize_tool_message_sequence(self.messages)
        self._refresh_system_prompt_for_turn(user_message)
        message_start = len(self.messages)
        self.messages.append(Message(role="user", content=user_message))
        await self._prepare_account_turn()
        self.messages[-1].message_id = self.llm.relay_turn_id
        tools = await self._tools_for_turn()

        try:
            accumulated_text = ""
            tool_calls: list[ToolCall] = []
            text_sanitizer = _StreamingTextSanitizer()

            self._begin_main_round()
            async for event in self.llm.chat_with_tools_stream(
                self.messages, self._remaining_tools(tools)
            ):
                if event.type == "text":
                    content = text_sanitizer.push(event.content)
                    if content:
                        accumulated_text += content
                        debug_log("agent.stream_text", content_length=len(content))
                        yield StreamEvent(type="text", content=content)
                elif event.type == "tool_call":
                    tool_calls.append(event.tool_call)
                    debug_log("agent.stream_tool_call", tool_name=event.tool_call.name)
                elif event.type == "references":
                    self._record_reference_urls(event.references)
                elif event.type == "done":
                    self._record_llm_usage(event.usage)

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
                injected = _inject_refs_if_missing(accumulated_text, self._ref_urls)
                if injected != accumulated_text:
                    extra = injected[len(accumulated_text) :]
                    yield StreamEvent(type="text", content=extra)
                    accumulated_text = injected
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

    def _reset_turn_vision_guard(self) -> None:
        self._vision_analyses_this_turn = 0
        self._successful_vision_images.clear()

    def _record_reference_urls(self, urls: list[str]) -> None:
        for url in urls:
            if isinstance(url, str) and _is_url(url) and url not in self._ref_urls:
                self._ref_urls.append(url)

    def _record_llm_references(self) -> None:
        self._record_reference_urls(getattr(self.llm, "last_search_references", []))

    def _vision_analysis_block_reason(self, arguments: dict) -> str | None:
        """Prevent costly visual-analysis loops within one user request."""
        raw_paths = arguments.get("image_paths", [])
        paths = raw_paths if isinstance(raw_paths, list) else []
        fingerprints = {
            str(Path(path).resolve()): _image_fingerprint(Path(path))
            for path in paths
            if isinstance(path, str)
        }
        if fingerprints and all(
            self._successful_vision_images.get(path) == fingerprint
            for path, fingerprint in fingerprints.items()
        ):
            return (
                "本轮已成功分析过这张未发生变化的图片。不要再次调用视觉模型，也不要继续执行脚本；"
                "请依据已有分析直接给用户结论。"
            )
        if self._vision_analyses_this_turn >= _MAX_VISION_ANALYSES_PER_TURN:
            return (
                f"本轮图片分析已达到 {_MAX_VISION_ANALYSES_PER_TURN} 次上限。"
                "不要再调用视觉模型或继续迭代脚本；请基于已有结果直接给用户结论。"
            )
        self._vision_analyses_this_turn += 1
        return None

    def _record_successful_vision_images(self, arguments: dict) -> None:
        raw_paths = arguments.get("image_paths", [])
        if not isinstance(raw_paths, list):
            return
        for path in raw_paths:
            if isinstance(path, str):
                resolved = str(Path(path).resolve())
                self._successful_vision_images[resolved] = _image_fingerprint(Path(path))

    def _is_official_mcp_tool(self, tool_name: str) -> bool:
        return self.config.llm.provider == "official" and tool_name in self._official_mcp_tool_names

    async def _call_official_mcp(self, tool_call: ToolCall) -> str:
        arguments = self._parse_tool_args(tool_call)

        async def execute():
            if self._official_mcp is None:
                return {"status": "failed", "is_error": True, "content": "官方搜索客户端未初始化。"}
            return await self._official_mcp.call(
                tool_call.name,
                arguments,
                turn_id=self.llm.relay_turn_id,
                call_id=tool_call.id,
            )

        result = await self.search_budget.execute(arguments, tool_call.id, execute)
        for url in _collect_ref_urls(result):
            if url not in self._ref_urls:
                self._ref_urls.append(url)
        return json.dumps(result, ensure_ascii=False, default=str)

    async def _handle_tool_calls(self, tool_calls: list[ToolCall], text: str) -> str:
        """Execute tool calls and feed results back to LLM for natural language response."""
        self.messages.append(Message(role="assistant", content=text or "", tool_calls=tool_calls))

        groups: dict[str, list[ToolCall]] = {}
        for tc in tool_calls:
            groups.setdefault(tc.name, []).append(tc)

        for tool_name, calls in groups.items():
            if self._is_official_mcp_tool(tool_name):
                for tc in calls:
                    result = await self._call_official_mcp(tc)
                    self.messages.append(Message(role="tool", content=result, tool_call_id=tc.id))
                continue
            spec = self.registry.get(tool_name)
            if spec is None:
                err = f"当前操作不可用：{_sanitize_user_facing_text(tool_name)}"
                for tc in calls:
                    self.messages.append(Message(role="tool", content=err, tool_call_id=tc.id))
                continue

            native_search_block = self._external_web_search_block_reason(tool_name)
            if native_search_block:
                debug_log("agent.external_web_search_blocked", tool_name=tool_name)
                for tc in calls:
                    self.messages.append(
                        Message(role="tool", content=native_search_block, tool_call_id=tc.id)
                    )
                continue

            reason = block_reason(tool_name, self.config.mode)
            if reason is not None:
                for tc in calls:
                    self.messages.append(Message(role="tool", content=reason, tool_call_id=tc.id))
                continue

            if spec.requires_confirmation and tool_name not in self.always_allow:
                all_args = [self._parse_tool_args(tc) for tc in calls]
                _needs_confirm = any(
                    _tool_call_needs_confirmation(tool_name, args) for args in all_args
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
                                    {
                                        "approved": False,
                                        "message": "用户选择暂不执行，继续留在规划模式完善方案。",
                                    },
                                    ensure_ascii=False,
                                )
                            else:
                                deny_msg = json.dumps(
                                    {"error": "用户拒绝执行此操作"}, ensure_ascii=False
                                )
                            self.messages.append(
                                Message(role="tool", content=deny_msg, tool_call_id=tc.id)
                            )
                        continue

            for tc in calls:
                parsed_args = self._parse_tool_args(tc)
                with (
                    use_token_tracker(self.tracker),
                    use_primary_model(self.config.llm.provider, self.config.llm.model),
                ):
                    exec_result = await self.runtime.execute(spec.function, parsed_args)
                if exec_result.success:
                    self._apply_runtime_config_update(tc.name, exec_result.result)
                    if self.config.mode == "execute" and tc.name in _BUILD_TOOLS:
                        from aero.data.plans import lock_current_plan

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

        from aero.toolbox.builtin_tools import get_vision_usage, reset_vision_usage

        vision_usage = get_vision_usage()
        if vision_usage:
            self.tracker.add_vision(
                vision_usage,
                self._effective_vision_identity()[1],
                provider=self._effective_vision_identity()[0],
            )
            reset_vision_usage()

        if self._direct_response:
            response = self._direct_response
            self.messages.append(Message(role="assistant", content=response))
            return response

        self._begin_main_round()
        response = _sanitize_user_facing_text(await self.llm.chat(self.messages))
        self._record_llm_references()
        self._record_llm_usage(self.llm.last_usage)
        response = _inject_refs_if_missing(response, self._ref_urls)
        self.messages.append(Message(role="assistant", content=response))
        return response

    async def _handle_tool_calls_stream(
        self,
        tool_calls: list[ToolCall],
        text: str,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Execute tool calls while streaming progress events."""
        self.messages.append(Message(role="assistant", content=text or "", tool_calls=tool_calls))

        pending_calls = tool_calls
        tools = self._allowed_tools() + self._official_mcp_tools

        for round_index in range(self.max_tool_rounds):
            debug_log(
                "agent.tool_round_started",
                round_index=round_index,
                pending_calls=len(pending_calls),
            )
            async for event in self._execute_tool_calls_stream(pending_calls):
                yield event

            from aero.toolbox.builtin_tools import get_vision_usage, reset_vision_usage

            vision_usage = get_vision_usage()
            if vision_usage:
                self.tracker.add_vision(
                    vision_usage,
                    self._effective_vision_identity()[1],
                    provider=self._effective_vision_identity()[0],
                )
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
            self._begin_main_round()
            async for event in self.llm.chat_with_tools_stream(
                self.messages, self._remaining_tools(tools)
            ):
                if event.type == "text":
                    content = text_sanitizer.push(event.content)
                    if content:
                        response += content
                        debug_log("agent.stream_text_after_tool", content_length=len(content))
                        yield StreamEvent(type="text", content=content)
                elif event.type == "tool_call":
                    pending_calls.append(event.tool_call)
                    debug_log("agent.stream_followup_tool_call", tool_name=event.tool_call.name)
                elif event.type == "references":
                    self._record_reference_urls(event.references)
                elif event.type == "done":
                    self._record_llm_usage(event.usage)

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
                    extra = injected[len(response) :]
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
            if self._is_official_mcp_tool(tool_name):
                for tc in calls:
                    yield StreamEvent(
                        type="status",
                        content="正在通过 Aerolytica 官方 MCP 搜索网页...",
                    )
                    result = await self._call_official_mcp(tc)
                    payload = json.loads(result)
                    failed = _tool_result_has_error_status(payload)
                    yield StreamEvent(
                        type="status",
                        content=(
                            "本轮搜索预算已用完"
                            if payload.get("status") == "blocked"
                            else "复用本轮搜索结果"
                            if payload.get("search_reused")
                            else "Aerolytica 官方网页搜索未成功"
                            if failed
                            else "Aerolytica 官方网页搜索完成"
                        ),
                    )
                    self.messages.append(Message(role="tool", content=result, tool_call_id=tc.id))
                continue
            if tool_name in {
                "delete_memo",
                "clear_memos",
            } and tool_name not in _destructive_memo_tools_for_text(self._current_user_message):
                err = "用户本轮没有明确要求删除备忘录，本次删除操作已阻止。"
                debug_log("agent.memo_delete_blocked", tool_name=tool_name)
                for tc in calls:
                    yield StreamEvent(type="status", content=err)
                    self.messages.append(Message(role="tool", content=err, tool_call_id=tc.id))
                continue
            spec = self.registry.get(tool_name)
            if spec is None:
                err = f"当前操作不可用：{_sanitize_user_facing_text(tool_name)}"
                debug_log("agent.tool_missing", tool_name=tool_name)
                for tc in calls:
                    yield StreamEvent(type="status", content=err)
                    self.messages.append(Message(role="tool", content=err, tool_call_id=tc.id))
                continue

            native_search_block = self._external_web_search_block_reason(tool_name)
            if native_search_block:
                debug_log("agent.external_web_search_blocked", tool_name=tool_name)
                for tc in calls:
                    self.messages.append(
                        Message(role="tool", content=native_search_block, tool_call_id=tc.id)
                    )
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
                    _tool_call_needs_confirmation(tool_name, args) for args in all_args
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
                    debug_log(
                        "agent.tool_confirmation_answered", tool_name=tool_name, choice=choice
                    )
                    if choice == "deny":
                        for tc in calls:
                            if tool_name == "propose_execution":
                                deny_msg = json.dumps(
                                    {
                                        "approved": False,
                                        "message": "用户选择暂不执行，继续留在规划模式完善方案。",
                                    },
                                    ensure_ascii=False,
                                )
                            else:
                                deny_msg = json.dumps(
                                    {"error": "用户拒绝执行此操作"}, ensure_ascii=False
                                )
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
        if tc.name == "analyze_image":
            blocked_reason = self._vision_analysis_block_reason(parsed_args)
            if blocked_reason:
                debug_log("agent.vision_analysis_blocked", reason=blocked_reason)
                yield StreamEvent(type="status", content=blocked_reason)
                self.messages.append(
                    Message(
                        role="tool",
                        content=json.dumps(
                            {"status": "blocked", "message": blocked_reason},
                            ensure_ascii=False,
                        ),
                        tool_call_id=tc.id,
                    )
                )
                return
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
        with (
            use_progress_reporter(reporter),
            use_token_tracker(self.tracker),
            use_primary_model(self.config.llm.provider, self.config.llm.model),
        ):
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
            if tc.name == "analyze_image":
                self._record_successful_vision_images(parsed_args)
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
            error_progress = _tool_error_progress_message(tc.name, parsed_args, exec_result.result)
            error_message = ""
            if isinstance(exec_result.result, dict):
                error_message = str(
                    exec_result.result.get("message") or exec_result.result.get("error") or ""
                )
                if exec_result.result.get("setup_required"):
                    credential_request = exec_result.result.get("credential_request", {})
                    yield StreamEvent(
                        type="status",
                        content=json.dumps(
                            {
                                "setup_required": exec_result.result["setup_required"],
                                "credential_request": credential_request,
                            },
                            ensure_ascii=False,
                        ),
                    )
            if error_message.startswith(error_progress):
                content = _sanitize_progress_text(error_message)
            else:
                content = f"{error_progress}：{_sanitize_progress_text(error_message)}"
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
            from aero.toolbox.builtin_tools import _find_config_path

            config_path = _find_config_path()
            if config_path.exists():
                fresh = AeroConfig.load(config_path)
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
        from aero.application.request_origin import current_origin
        from aero.toolbox.approval_input import request_approval_from_context

        if current_origin() == "web":
            return await request_approval_from_context({"tool": tool_name, "args": args})
        print(f"\n⚠️  危险操作: {tool_name}")
        args_summary = json.dumps(args, ensure_ascii=False, default=str)
        if len(args_summary) > 200:
            args_summary = args_summary[:200] + "..."
        print(f"   参数: {args_summary}")
        print("   此操作不可撤销，是否允许？")
        print("   [y] 允许  [a] 一直允许  [n] 拒绝")
        return await self._read_terminal_confirmation()

    async def _ask_confirmation_simple_batch(self, tool_name: str, all_args: list[dict]) -> str:
        from aero.application.request_origin import current_origin
        from aero.toolbox.approval_input import request_approval_from_context

        if current_origin() == "web":
            return await request_approval_from_context({"tool": tool_name, "calls": all_args})
        print(f"\n⚠️  危险操作: {tool_name}（共 {len(all_args)} 次调用）")
        for i, args in enumerate(all_args, 1):
            summary = json.dumps(args, ensure_ascii=False, default=str)
            if len(summary) > 200:
                summary = summary[:200] + "..."
            print(f"   #{i}: {summary}")
        print("   此操作不可撤销，是否允许所有？")
        print("   [y] 允许全部  [a] 一直允许  [n] 拒绝全部")

        return await self._read_terminal_confirmation()

    async def _read_terminal_confirmation(self) -> str:
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
        "search_cams_variables": "正在查询 CAMS 可用变量...",
        "get_cams_latest_forecast_cycle": "正在查询 CAMS 最新可用起报...",
        "search_gfs_variables": "正在查询 GFS 可用要素...",
        "lookup_gfs_parameter": "正在查阅 GFS 要素定义...",
        "check_gfs_availability": "正在检查 GFS 可用时次...",
        "inspect_gfs_inventory": "正在查看 GFS 文件库存...",
        "lookup_ecmwf_parameter": "正在查阅 ECMWF 参数定义...",
        "describe_cds_dataset": "正在读取数据集说明...",
        "download_era5": "正在准备并提交数据下载任务...",
        "download_cams": "正在准备并提交 CAMS 数据请求...",
        "subset_netcdf": "正在裁剪 NetCDF 文件...",
        "download_gfs": "正在准备 GFS 预报数据下载...",
        "list_downloads": "正在查看下载任务记录...",
        "query_download": "正在查询下载任务状态...",
        "retry_download": "正在重试下载任务...",
        "cleanup_downloads": "正在清理下载记录...",
        "check_cds_config": "正在检查 CDS 配置...",
        "configure_cds_key": "正在保存 CDS 配置...",
        "check_ads_config": "正在检查 ADS 配置...",
        "configure_ads_key": "正在保存 ADS 配置...",
        "check_earthdata_config": "正在检查 Earthdata 配置...",
        "configure_earthdata_token": "正在保存 Earthdata 配置...",
        "configure_email_config": "正在保存邮箱配置...",
        "check_email_config": "正在检查邮箱配置...",
        "send_email": "正在发送邮件...",
        "clear_cds_config": "正在清除 CDS 配置...",
        "ensure_runtime_tools": "正在配置运行时命令行工具...",
        "inspect_nc": "正在检查数据文件...",
        "inspect_grib2": "正在检查 GRIB2 文件...",
        "scan_local_files": "正在扫描本地科研数据...",
        "list_files": "正在查看文件列表...",
        "list_figures": "正在查看图片列表...",
        "delete_file": "正在删除文件...",
        "search_literature": "正在检索学术文献...",
        "search_web": "正在调用外部网页搜索服务...",
        "save_literature": "正在保存文献信息...",
        "download_literature_pdf": "正在下载文献全文...",
        "list_literature": "正在查看已保存文献...",
        "read_pdf": "正在提取 PDF 文本内容...",
        "preview_pdf": "正在打开 PDF...",
        "record_instruction": "正在记录个性化指令...",
        "show_instructions": "正在查看已有指令...",
        "clear_instructions": "正在清空指令记录...",
        "record_memo": "正在准备研究备忘录...",
        "show_memos": "正在查看研究备忘录...",
        "update_memo": "正在准备更新研究备忘录...",
        "delete_memo": "正在删除研究备忘录...",
        "clear_memos": "正在清理研究备忘录...",
        "initialize_paper_versioning": "正在启用论文版本管理...",
        "paper_version_status": "正在查看论文版本状态...",
        "save_paper_version": "正在保存论文版本...",
        "list_paper_versions": "正在查看论文版本历史...",
        "diff_paper_version": "正在比较论文版本...",
        "restore_paper_version": "正在恢复论文版本...",
        "export_paper": "正在转换论文格式...",
        "write_plan_document": "正在保存计划书...",
    }
    return messages.get(tool_name, "正在处理请求...")


def _tool_done_message(tool_name: str) -> str:
    messages = {
        "search_cds_variables": "数据变量信息查询完成",
        "search_cams_variables": "CAMS 可用变量查询完成",
        "get_cams_latest_forecast_cycle": "CAMS 最新可用起报查询完成",
        "search_gfs_variables": "GFS 可用要素查询完成",
        "lookup_gfs_parameter": "GFS 要素定义查询完成",
        "check_gfs_availability": "GFS 可用时次检查完成",
        "inspect_gfs_inventory": "GFS 文件库存查看完成",
        "lookup_ecmwf_parameter": "ECMWF 参数定义查询完成",
        "describe_cds_dataset": "数据集说明读取完成",
        "download_era5": "数据下载处理完成",
        "download_cams": "CAMS 数据下载完成",
        "subset_netcdf": "NetCDF 文件裁剪完成",
        "download_gfs": "GFS 预报数据下载完成",
        "list_downloads": "下载任务记录已读取",
        "query_download": "下载任务状态已读取",
        "retry_download": "下载任务重试完成",
        "cleanup_downloads": "下载记录清理完成",
        "check_cds_config": "CDS 配置检查完成",
        "configure_cds_key": "CDS 配置已保存",
        "check_ads_config": "ADS 配置检查完成",
        "configure_ads_key": "ADS 配置已保存",
        "check_earthdata_config": "Earthdata 配置检查完成",
        "configure_earthdata_token": "Earthdata 配置已保存",
        "configure_email_config": "邮箱配置已保存",
        "check_email_config": "邮箱配置检查完成",
        "send_email": "邮件已发送",
        "clear_cds_config": "CDS 配置已清除",
        "ensure_runtime_tools": "运行时命令行工具配置完成",
        "inspect_nc": "数据文件检查完成",
        "inspect_grib2": "GRIB2 文件检查完成",
        "scan_local_files": "本地数据扫描完成",
        "list_files": "文件列表读取完成",
        "list_figures": "图片列表读取完成",
        "delete_file": "文件删除完成",
        "search_literature": "学术文献检索完成",
        "search_web": "外部网页搜索完成",
        "save_literature": "文献信息保存完成",
        "download_literature_pdf": "文献全文下载完成",
        "list_literature": "已保存文献列表读取完成",
        "read_pdf": "PDF 文本内容提取完成",
        "preview_pdf": "PDF 已打开",
        "record_instruction": "个性化指令已记录",
        "show_instructions": "已有指令查看完成",
        "clear_instructions": "指令记录已清空",
        "record_memo": "研究备忘录已记录",
        "show_memos": "研究备忘录查看完成",
        "update_memo": "研究备忘录已更新",
        "delete_memo": "研究备忘录已删除",
        "clear_memos": "研究备忘录已清理",
        "initialize_paper_versioning": "论文版本管理已启用",
        "paper_version_status": "论文版本状态查看完成",
        "save_paper_version": "论文版本已保存",
        "list_paper_versions": "论文版本历史查看完成",
        "diff_paper_version": "论文版本比较完成",
        "restore_paper_version": "论文版本已恢复",
        "export_paper": "论文文件已生成",
        "write_plan_document": "计划书已保存",
    }
    return messages.get(tool_name, "处理完成")


def _tool_error_prefix(tool_name: str) -> str:
    messages = {
        "download_era5": "数据下载处理失败",
        "download_cams": "CAMS 数据下载失败",
        "subset_netcdf": "NetCDF 文件裁剪失败",
        "download_gfs": "GFS 预报数据下载失败",
        "retry_download": "下载任务重试失败",
        "search_cds_variables": "数据变量信息查询失败",
        "search_cams_variables": "CAMS 可用变量查询失败",
        "get_cams_latest_forecast_cycle": "CAMS 最新可用起报查询失败",
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
        "check_ads_config": "ADS 配置检查失败",
        "configure_ads_key": "ADS 配置保存失败",
        "check_earthdata_config": "Earthdata 配置检查失败",
        "configure_earthdata_token": "Earthdata 配置保存失败",
        "configure_email_config": "邮箱配置保存失败",
        "check_email_config": "邮箱配置检查失败",
        "send_email": "邮件发送失败",
        "clear_cds_config": "CDS 配置清除失败",
        "ensure_runtime_tools": "运行时命令行工具配置失败",
        "inspect_nc": "数据文件检查失败",
        "inspect_grib2": "GRIB2 文件检查失败",
        "scan_local_files": "本地数据扫描失败",
        "list_files": "文件列表读取失败",
        "list_figures": "图片列表读取失败",
        "delete_file": "文件删除失败",
        "search_literature": "学术文献检索失败",
        "search_web": "外部网页搜索失败",
        "save_literature": "文献信息保存失败",
        "download_literature_pdf": "文献全文下载失败",
        "list_literature": "已保存文献列表读取失败",
        "read_pdf": "PDF 文本内容提取失败",
        "preview_pdf": "打开 PDF 失败",
        "record_instruction": "记录指令失败",
        "show_instructions": "查看指令失败",
        "clear_instructions": "清空指令失败",
        "record_memo": "记录备忘录失败",
        "show_memos": "查看备忘录失败",
        "update_memo": "更新备忘录失败",
        "delete_memo": "删除备忘录失败",
        "clear_memos": "清理备忘录失败",
        "initialize_paper_versioning": "启用论文版本管理失败",
        "paper_version_status": "查看论文版本状态失败",
        "save_paper_version": "保存论文版本失败",
        "list_paper_versions": "查看论文版本历史失败",
        "diff_paper_version": "比较论文版本失败",
        "restore_paper_version": "恢复论文版本失败",
        "export_paper": "论文格式转换失败",
        "write_plan_document": "计划书保存失败",
    }
    return messages.get(tool_name, "处理失败")
