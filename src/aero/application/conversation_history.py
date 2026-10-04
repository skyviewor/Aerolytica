"""Export visible conversation history independently of the model context."""

from __future__ import annotations

import httpx

from aero.application.chat_images import describe_images


def sync_error_reason(response: httpx.Response, fallback: str) -> str:
    """Keep protocol/version failures distinct from actual context divergence."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return fallback
    if isinstance(detail, str) and detail.strip():
        return detail[:200]
    if isinstance(detail, dict):
        return {
            "context_version_conflict": "云端上下文版本已更新，请重新点击同步",
            "context_rotation_in_progress": "上下文归档正在进行，请稍后重试同步",
            "context_object_position_conflict": "云端上下文写入尚未完成，请稍后重试同步",
            "context_non_append_change": "本地与云端上下文内容发生分叉，无法追加同步",
            "context_missing_before_compaction": "缺少压缩前上下文，无法同步压缩归档",
        }.get(detail.get("code"), fallback)
    return fallback


def visible_history(payload: dict) -> list[dict]:
    """Prefer the saved display transcript, excluding presets and tool messages."""
    transcript = (payload.get("meta") or {}).get("transcript")
    if not transcript:
        transcript = []
        summary_ack = False
        for message in payload.get("messages", []):
            role, content = message.get("role"), message.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            marker = next(
                (
                    prefix
                    for prefix in ("[compact_summary]", "[Previous conversation summary]")
                    if role == "user" and content.startswith(prefix)
                ),
                None,
            )
            if marker:
                transcript.append({"role": "summary", "content": content[len(marker) :].strip()})
                summary_ack = True
                continue
            if summary_ack and role == "assistant":
                summary_ack = False
                continue
            summary_ack = False
            transcript.append({"role": role, "content": content})
    result = []
    for item in transcript:
        if not isinstance(item, dict):
            continue
        role, content = item.get("role"), item.get("content")
        if role not in {"user", "assistant", "summary"} or not isinstance(content, str):
            continue
        if content.strip():
            result.append(
                {
                    "role": role,
                    "content": content,
                    "images": describe_images(content, f"history:{len(result)}")
                    if role == "assistant"
                    else [],
                }
            )
    return result
