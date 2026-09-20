"""Tests for automatic session-title eligibility."""

from aero.application.session_titles import (
    has_successful_first_exchange,
    session_title_prompt,
)
from aero.core.types import Message


def test_successful_first_exchange_requires_real_assistant_content() -> None:
    assert has_successful_first_exchange(
        [Message(role="user", content="你好"), Message(role="assistant", content="你好！")]
    )
    assert not has_successful_first_exchange(
        [Message(role="user", content="你好"), Message(role="assistant", content="")]
    )
    assert not has_successful_first_exchange(
        [
            Message(role="user", content="你好"),
            Message(role="assistant", content="抱歉，出错了：模型服务返回错误"),
        ]
    )


def test_title_uses_first_successful_exchange_after_a_failed_turn() -> None:
    messages = [
        Message(role="user", content="第一次请求"),
        Message(role="assistant", content="抱歉，出错了：上游拒绝请求"),
        Message(role="user", content="分析华北气温"),
        Message(role="assistant", content="已完成华北气温分析"),
    ]

    assert has_successful_first_exchange(messages)
    prompt = session_title_prompt(messages, "zh")
    assert "分析华北气温" in prompt
    assert "上游拒绝请求" not in prompt
