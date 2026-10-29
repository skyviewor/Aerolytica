"""User-feedback tools."""

import hashlib
import json
import platform
import re
from urllib.parse import urlencode

import httpx

from aero.application.turn_context import current_turn_id
from aero.core.official_account import (
    OfficialAccountError,
    OfficialAccountSession,
    OfficialLoginRequiredError,
)
from aero.toolbox.registry import register_tool
from aero.version import get_version

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


def _feedback_text(value: str) -> str:
    """Suggestions need no private file locations, including their basenames."""
    value = re.sub(
        r"(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        value,
    )
    value = re.sub(
        r"(?i)([\"'](?:api[_ -]?key|access_token|refresh_token|token|secret|password)[\"']"
        r"\s*:\s*)([\"'])(.*?)\2",
        r'\1"[REDACTED]"',
        value,
    )
    value = re.sub(
        r"\b(?:ghp_|github_pat_|glpat-|rly_|agt_)[A-Za-z0-9_-]{8,}\b"
        r"|\b(?:AKIA|ASIA|LTAI)[A-Z0-9a-z]{12,}\b"
        r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "[REDACTED TOKEN]",
        value,
    )
    value = _redact_issue_text(value)
    value = re.sub(r"(?:/Users/|/home/|~/)[^\s，。；、<>\"']+", "[PRIVATE PATH]", value)
    return re.sub(r"(?i)\b[A-Z]:\\(?:Users\\)?[^\s，。；、<>\"']+", "[PRIVATE PATH]", value)


@register_tool(
    name="submit_product_feedback",
    description=(
        "向 Aerolytica 官方提交单向产品建议，不是客服工单，不保证逐条回复。"
        "用户明确要求帮我反馈/提交建议即表示同意，直接调用，不再弹审批框；"
        "仅表达不满或建议时先询问一次是否愿意提交，未获同意不得调用。"
        "只概括用户实际表达的内容，不编造复现步骤，不复制完整对话、日志或文件。"
        "自动脱敏并附带真实版本和系统类型，须官方登录，使用自备模型也可提交。"
        "只有 received 成功回执才可说已收录；结果不明确不得自动重复投递或声称成功。"
        "用户明确要求 GitHub Issue 时改用 prepare_issue_link。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "建议标题，1–200 字符。"},
            "content": {"type": "string", "description": "建议正文，1–10000 字符，仅必要信息。"},
            "category": {
                "type": "string",
                "enum": ["feature", "experience", "bug", "other"],
                "description": "feature 功能建议，experience 体验建议，bug 问题，other 其他。",
            },
        },
        "required": ["title", "content", "category"],
        "additionalProperties": False,
    },
)
async def submit_product_feedback(title: str, content: str, category: str) -> dict:
    """Submit without consulting a model, billing, tickets, or notifications."""
    if not isinstance(title, str) or not isinstance(content, str):
        return {"status": "error", "message": "标题和正文必须是文本。"}
    # Validate before AND after redaction; never silently truncate a suggestion.
    if not 1 <= len(title.strip()) <= 200 or not 1 <= len(content.strip()) <= 10000:
        return {"status": "error", "message": "标题须为 1–200 字符，正文须为 1–10000 字符。"}
    title, content = _feedback_text(title), _feedback_text(content)
    if len(title) > 200 or len(content) > 10000:
        return {"status": "error", "message": "脱敏后的内容过长，请简化建议。"}
    if not isinstance(category, str) or category not in {"feature", "experience", "bug", "other"}:
        return {"status": "error", "message": "无效的建议分类。"}
    turn_id = current_turn_id()
    if not turn_id:
        return {"status": "error", "message": "缺少当前消息标识，请在对话中重新确认提交。"}
    identity = json.dumps([turn_id, title, content, category], ensure_ascii=False)
    os_type = platform.system()
    payload = {
        "request_id": "feedback_" + hashlib.sha256(identity.encode()).hexdigest(),
        "title": title,
        "content": content,
        "category": category,
        "client_version": get_version(),
        "os_type": os_type if os_type in {"Darwin", "Linux", "Windows"} else "unknown",
    }
    session = OfficialAccountSession()
    try:
        response = await session.request("POST", "/v1/feedback", json=payload)
        if response.status_code in {401, 403}:
            raise OfficialLoginRequiredError()
        errors = {
            404: "官方建议接口尚未部署，未提交建议。",
            409: "建议请求标识冲突，未提交新建议，不要自动重复投递。",
            422: "建议内容未通过服务端校验，未提交建议。",
            429: "今天提交的产品建议已达上限，请明天再试。",
        }
        if response.status_code in errors:
            return {"status": "error", "message": errors[response.status_code]}
        if response.status_code not in {200, 201}:
            return _uncertain_feedback()
        data = response.json()
        if (
            not isinstance(data, dict)
            or data.get("received") is not True
            or not isinstance(data.get("feedback_id"), str)
            or not re.fullmatch(r"fb_[a-f0-9]{32}", data["feedback_id"])
        ):
            return _uncertain_feedback()
        return {
            "status": "success",
            "received": True,
            "feedback_id": data["feedback_id"],
            "message": "建议已收录；不保证逐条回复。",
        }
    except OfficialLoginRequiredError:
        return {
            "status": "error",
            "message": "请先使用 /login 登录官方账户，不要在对话里提供密码。",
        }
    except (OfficialAccountError, httpx.HTTPError, ValueError):
        return _uncertain_feedback()
    finally:
        await session.close()


def _uncertain_feedback() -> dict:
    return {
        "status": "unknown",
        "received": False,
        "message": "未取得建议收录回执，提交结果不明确；不要声称已提交，也不要自动重复投递。",
    }


@register_tool(
    name="prepare_issue_link",
    description=(
        "为 Aerolytica 项目生成一个预填标题和正文的 GitHub Issue 链接。"
        "仅当用户已经明确同意通过 GitHub Issue 反馈后调用；普通反馈用 submit_product_feedback。"
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
