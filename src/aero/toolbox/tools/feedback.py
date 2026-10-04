"""User-feedback tools."""

import re
from urllib.parse import urlencode

from aero.toolbox.registry import register_tool

_ISSUES_NEW_URL = "https://github.com/skyviewor/Aerolytica/issues/new"
_REDACTION_RULES = (
    (re.compile(r"\b(?:nvapi|sk)-[A-Za-z0-9_-]{8,}\b"), "[REDACTED API KEY]"),
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*\b"),
        "Bearer [REDACTED TOKEN]",
    ),
    (
        re.compile(r"(?i)\b(api[_ -]?key|token|secret|password)\s*[:=]\s*\S+"),
        r"\1: [REDACTED]",
    ),
    (
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
        "[REDACTED EMAIL]",
    ),
    (re.compile(r"/Users/[^/\s]+"), "/Users/[USER]"),
    (re.compile(r"/home/[^/\s]+"), "/home/[USER]"),
)


def _redact_issue_text(value: str) -> str:
    """Remove common credentials and personal identifiers from issue text."""
    redacted = value.strip()
    for pattern, replacement in _REDACTION_RULES:
        redacted = pattern.sub(replacement, redacted)
    return redacted


@register_tool(
    name="prepare_issue_link",
    description=(
        "为 Aerolytica 项目生成一个预填标题和正文的 GitHub Issue 链接。"
        "仅当用户已经明确同意提交反馈后调用；不得在询问用户是否愿意反馈之前调用。"
        "Bug 应根据对话提炼实际表现、最小复现步骤、期望结果和必要环境，不要复制整段对话。"
        "内容会再次自动脱敏。此工具不会提交 Issue，用户打开链接后仍需检查并手动提交。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "简洁、具体的 Issue 标题，不要包含情绪化措辞。",
            },
            "problem": {
                "type": "string",
                "description": "用户遇到的问题或未被满足的需求。",
            },
            "expected": {
                "type": "string",
                "description": "用户期望的行为或结果。",
            },
            "actual": {
                "type": "string",
                "description": "当前实际行为、错误信息或缺失能力；未知时留空。",
            },
            "reproduction_steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "按发生顺序整理的最小复现步骤；不能确定时留空。",
            },
            "context": {
                "type": "string",
                "description": "版本、平台、数据源、模型等有助于定位的信息；未知时留空。",
            },
        },
        "required": ["title", "problem", "expected"],
    },
)
async def prepare_issue_link(
    title: str,
    problem: str,
    expected: str,
    actual: str = "",
    reproduction_steps: list[str] | None = None,
    context: str = "",
) -> dict:
    """Return a GitHub URL with a reviewable issue draft."""
    clean_title = _redact_issue_text(title)
    clean_problem = _redact_issue_text(problem)
    clean_expected = _redact_issue_text(expected)
    clean_actual = _redact_issue_text(actual)
    clean_steps = [
        _redact_issue_text(step) for step in (reproduction_steps or []) if _redact_issue_text(step)
    ]
    clean_context = _redact_issue_text(context)
    if not clean_title or not clean_problem or not clean_expected:
        raise ValueError("Issue 标题、问题描述和期望结果不能为空。")

    sections = [
        "## 问题或需求",
        clean_problem,
        "",
        "## 期望结果",
        clean_expected,
    ]
    if clean_actual:
        sections.extend(("", "## 当前表现", clean_actual))
    if clean_steps:
        sections.extend(("", "## 复现步骤"))
        sections.extend(f"{index}. {step}" for index, step in enumerate(clean_steps, start=1))
    if clean_context:
        sections.extend(("", "## 补充信息", clean_context))
    sections.extend(
        (
            "",
            "---",
            "此 Issue 由 Aerolytica 根据用户确认的反馈内容生成，提交前可继续编辑。",
        )
    )
    body = "\n".join(sections)
    query = urlencode({"title": clean_title, "body": body})
    # Textual decodes Markdown hrefs once before handing them to the system
    # browser. Protect percent escapes so the browser still receives a valid
    # UTF-8 query, including encoded Markdown characters such as ``#``.
    issue_url = f"{_ISSUES_NEW_URL}?{query.replace('%', '%25')}"
    return {
        "success": True,
        "issue_url": issue_url,
        "title": clean_title,
        "body": body,
        "message": "Issue 草稿链接已生成；打开后检查内容并手动提交。",
    }
