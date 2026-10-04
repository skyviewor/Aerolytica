"""Cloud-visible operation descriptions, without file contents or credentials."""

from __future__ import annotations

import re


def approval_payload(data: dict) -> dict:
    from aero.toolbox.tools.runtime import _redact_shell_command

    tool = str(data.get("tool") or "operation")[:120]
    titles = {
        "run_shell": "执行本地命令",
        "write_file": "写入本地文件",
        "delete_file": "删除本地文件",
        "propose_execution": "开始执行方案",
    }
    title = titles.get(tool, f"执行本地操作：{tool}")
    details = []
    arguments = data.get("batch_args")
    if not isinstance(arguments, list):
        arguments = [data.get("args") or {}]
    # Leave room for UTF-8 expansion and JSON overhead within Relay's 20 KB limit.
    budget = 4000
    omitted = False
    for index, args in enumerate(arguments):
        if not isinstance(args, dict):
            continue
        fields = [
            ("command", "执行命令"),
            ("workdir", "工作目录"),
            ("file_path", "目标文件"),
            ("path", "目标路径"),
        ]
        for key, label in fields:
            if key not in args:
                continue
            value = _redact_shell_command(str(args[key]))
            value = re.sub(
                r"(?i)([\w-]*(?:key|token|password|secret)\s*[=:]\s*)"
                r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
                r"\1[已隐藏]",
                value,
            )
            value = re.sub(
                r"(?i)(--(?:api[-_]key|token|password|secret)\s+)"
                r"(?:\"[^\"]*\"|'[^']*'|\S+)",
                r"\1[已隐藏]",
                value,
            )
            value = re.sub(r"(https?://)[^/\s@]+@", r"\1[已隐藏]@", value)
            if len(value) > budget:
                omitted = True
                value = value[: max(0, budget)] + "…（内容过长，已截断）"
            details.append(
                {
                    "label": f"操作 {index + 1} · {label}" if len(arguments) > 1 else label,
                    "value": value,
                }
            )
            budget -= len(value)
            if budget <= 0:
                omitted = True
                break
        if budget <= 0:
            break
    description = "批准后将在客户端执行下列操作；拒绝不会执行该操作。"
    if tool == "propose_execution":
        description = "批准后将从规划模式切换到执行模式，开始执行已确认的方案。"
    elif not details:
        description = f"客户端请求执行 {tool}，但未提供可展示的操作详情，请勿盲目批准。"
    return {
        "tool": tool,
        "title": title,
        "description": description,
        "details": details,
        "details_truncated": omitted,
    }
