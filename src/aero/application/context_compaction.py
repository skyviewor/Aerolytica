"""Shared conversation compaction semantics for chat and resident agents."""

from __future__ import annotations

from aero.core.types import Message


def summary_prompt_for(messages: list[Message]) -> str:
    prompt = (
        "Summarize the full conversation below so future turns can continue "
        "from the summary alone. Remove unimportant chatter and redundant detail. "
        "Keep user goals, decisions, constraints, current task state, completed work, "
        "tool results, file paths, URLs, data names, numerical values, errors, and any "
        "preferences the user expressed. Be concise but complete.\n\n"
    )
    for message in messages[1:]:
        content = (message.content or "")[:2000]
        if message.tool_calls:
            content += "\n[tool_calls: " + ", ".join(call.name for call in message.tool_calls) + "]"
        prompt += f"[{message.role}]: {content}\n\n"
    return prompt + "\nNow provide the compacted context summary only."


def compacted_messages(system_message: Message, summary: str) -> list[Message]:
    return [
        system_message,
        Message(role="user", content=f"[compact_summary]\n{summary}"),
        Message(role="assistant", content="OK, I understand the context above."),
    ]
