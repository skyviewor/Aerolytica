"""Cloud-visible operation descriptions, without file contents or credentials."""

from __future__ import annotations

import re


def approval_payload(data: dict) -> dict:
    from aero.toolbox.tools.runtime import _redact_shell_command

    tool = str(data.get("tool") or "operation")[:120]
    if tool in {"set_cloud_sync_enabled", "sync_cloud_now"}:
        args = data.get("args") or {}
        action = args.get("action")
        title = {
            "enable": "开启自动云同步",
            "disable": "关闭自动云同步",
            "run_once": "立即执行一次云同步",
        }.get(action, "云同步控制")
        return {
            "tool": tool,
            "title": title,
            "description": (
                "仅批准本次操作。同步是双向的，可能上传、下载或传播删除；冲突不自动覆盖。"
                "开启或单次同步可能启动现有本机常驻服务，关闭聊天后仍可运行，"
                "但不配置开机自启。关闭会取消本服务的自动/手动同步任务，不停止聊天 Agent。"
                "修改开关或间隔可能中断在途传输；待处理状态保留。"
                "单次同步不改变自动开关，工作区忙时排队，排队不代表完成。"
            ),
            "details": [
                {"label": "工作区", "value": str(args.get("workspace") or "")},
                {"label": "云项目", "value": str(args.get("project_id") or "未绑定")},
                {"label": "同步目录", "value": "、".join(args.get("includes") or [])},
                {
                    "label": "检查间隔（秒）",
                    "value": str(
                        args.get("interval_seconds") or args.get("previous_interval_seconds", 30)
                    ),
                },
            ],
            "details_truncated": False,
        }
    if tool == "set_local_sync_directories":
        args = data.get("args") or {}
        return {
            "tool": tool,
            "title": "修改本地云同步范围",
            "description": (
                "仅批准本次配置修改，不更改云项目或账户，不启用或立即运行同步。"
                "新增目录在后续双向同步中可能上传、下载或传播删除；移除目录仅退出"
                "同步，不删除两端现有文件。未绑定时只保存后续默认选择。"
                "暂停状态和硬性安全排除保持不变；配置变化后必须重新审批。"
            ),
            "details": [
                {"label": label, "value": ("、".join(args.get(key) or []) or "（无）")}
                for key, label in (
                    ("previous_includes", "原同步目录"),
                    ("includes", "新同步目录"),
                    ("added", "新增目录"),
                    ("removed", "移除目录"),
                )
            ]
            + [
                {"label": "工作区", "value": str(args.get("workspace") or "")},
                {"label": "云项目", "value": str(args.get("project_id") or "未绑定")},
                {"label": "暂停状态", "value": "保持暂停" if args.get("paused") else "不改变"},
            ],
            "details_truncated": False,
        }
    if tool == "delete_cloud_files":
        args = data.get("args") or {}
        files = args.get("files") or []
        return {
            "tool": tool,
            "title": f"删除 {len(files)} 个云端文件",
            "description": (
                "仅批准本次列出的云文件删除，不删除云目录或对话，也不直接修改本地文件。"
                "若已开启双向同步，后续同步可能将云端删除反映到本地。"
                "会话附件删除后，旧对话可能无法显示对应图片。删除按服务端保留策略处理，"
                "不保证永久可恢复。拒绝不删除；后续删除仍须重新审批。"
            ),
            "details": [
                {"label": "云项目 ID", "value": str(args.get("project_id") or "")},
                {"label": "文件数", "value": str(len(files))},
                *[
                    {
                        "label": f"文件 {index + 1}",
                        "value": (
                            f"{item['relative_path']} · {item['size_bytes']} 字节\n"
                            f"ID: {item['file_id']} · 版本: {item['expected_version']}"
                        ),
                    }
                    for index, item in enumerate(files)
                ],
            ],
            "details_truncated": False,
        }
    titles = {
        "run_shell": "执行本地命令",
        "write_file": "写入本地文件",
        "delete_file": "删除本地文件",
        "propose_execution": "开始执行方案",
        "upload_cloud_file": "上传文件到官方云空间",
        "download_cloud_file": "从官方云空间下载文件",
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
            ("project_id", "云项目 ID"),
            ("directory_id", "云目录 ID"),
            ("file_id", "云文件 ID"),
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
