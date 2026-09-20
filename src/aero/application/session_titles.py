"""Shared automatic session-title helpers for TUI and Web sessions."""

from __future__ import annotations

import re

from aero.core.types import Message


def _first_successful_exchange(messages: list[Message]) -> tuple[str, str] | None:
    pending_user = ""
    for message in messages:
        content = message.content.strip()
        if message.role == "user" and content:
            pending_user = content
            continue
        if message.role == "assistant" and pending_user:
            if content and not content.startswith("抱歉，出错了："):
                return pending_user, content
            pending_user = ""
    return None


def has_successful_first_exchange(messages: list[Message]) -> bool:
    """Return true when at least one user turn has a real assistant answer."""
    return _first_successful_exchange(messages) is not None


def session_title_prompt(messages: list[Message], language: str) -> str:
    exchange = _first_successful_exchange(messages)
    text = "无有效对话内容"
    if exchange is not None:
        user_text, assistant_text = exchange
        text = (
            f"用户: {clean_session_title_prompt_text(user_text)}\n"
            f"Aero: {clean_session_title_prompt_text(assistant_text)}"
        )
    if language == "zh":
        return (
            "请根据下面第一轮对话为这个会话起一个简短标题。\n"
            "要求：中文优先，8到18个字；不要加引号；不要解释；不要句号。\n\n"
            f"{text}\n\n标题："
        )
    return (
        "Create a short title for this chat from the first exchange below.\n"
        "Requirements: 3 to 8 words, no quotes, no explanation, no trailing period.\n\n"
        f"{text}\n\nTitle:"
    )


def fallback_session_title(messages: list[Message], max_len: int = 24) -> str:
    for message in messages:
        if message.role != "user" or not message.content.strip():
            continue
        text = clean_session_title_text(message.content)
        if text and not is_low_information_session_title(text):
            return truncate_session_title(text, max_len)
    return ""


def normalize_session_title(title: str, max_len: int = 24) -> str:
    title = title.strip()
    title = title.splitlines()[0] if title else ""
    title = re.sub(r"^(?:标题|title)\s*[:：]\s*", "", title, flags=re.I)
    title = title.strip(" \t\r\n\"'“”‘’`*#。，、；：:,.!?！？")
    title = re.sub(r"\s+", " ", title)
    return truncate_session_title(title, max_len) if title else ""


def clean_session_title_prompt_text(text: str) -> str:
    text = re.sub(r"!\[[^]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def clean_session_title_text(text: str) -> str:
    text = clean_session_title_prompt_text(text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[/\\][^\s，。！？；：,!?;:]+", " ", text)
    text = text.strip(" \t\r\n\"'“”‘’。，、；：:,.!?！？")
    text = re.sub(
        r"^(?:请|帮我|麻烦|能不能|可以|能否|请帮我|帮忙|我想|我要|给我)\s*",
        "",
        text,
    ).strip()
    return re.sub(r"^(?:please|can you|could you|help me|i want to)\s+", "", text, flags=re.I)


def is_low_information_session_title(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text).lower()
    if not normalized:
        return True
    if normalized in {"你好", "您好", "嗨", "哈喽", "hello", "hi", "hey", "在吗", "test", "测试"}:
        return True
    return len(normalized) <= 2 and not re.search(r"[\u4e00-\u9fff]", normalized)


def truncate_session_title(text: str, max_len: int) -> str:
    text = text.strip()
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"
