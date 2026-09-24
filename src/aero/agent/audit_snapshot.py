"""Human-readable, redacted exports of persisted Agent conversations."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

from aero.agent.session import SessionManager, _redact_secret_value

MAX_TEXT_BYTES = 10 * 1024 * 1024


def render_conversation(manager: SessionManager, session_id: str) -> str:
    """Read one coherent saved session, never the mutable in-flight Agent state."""
    loaded = manager.load(session_id)
    if loaded is None:
        raise ValueError("本地没有该智能体的已保存会话，或本地密钥无法解密。")
    messages, meta = loaded
    lines = [
        "Aerolytica 智能体上下文审计快照",
        f"会话 ID：{session_id}",
        f"名称：{meta.name or '未命名智能体'}",
        f"模型：{meta.model or '未知'}",
        f"模式：{meta.mode or '未知'}",
        f"会话创建时间：{datetime.fromtimestamp(meta.created_at, timezone.utc).isoformat() if meta.created_at else '未知'}",
        f"最后保存时间：{datetime.fromtimestamp(meta.updated_at, timezone.utc).isoformat() if meta.updated_at else '未知'}",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
        "说明：此文件包含已持久化上下文；已识别的凭据会被遮盖，仍请在上传前人工审阅。",
        "",
    ]
    for index, message in enumerate(messages, start=1):
        lines.append(f"===== {index}. {message.role.upper()} =====")
        if message.tool_call_id:
            lines.append(f"工具调用 ID：{message.tool_call_id}")
        if message.content:
            lines.append(str(_redact_secret_value(message.content)))
        for call in message.tool_calls or []:
            lines.append(f"工具：{call.name}（{call.id}）")
            lines.append(json.dumps(_redact_secret_value(call.arguments), ensure_ascii=False, indent=2))
        lines.append("")
    result = "\n".join(lines)
    if len(result.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("审计快照超过 10 MB 上限，请先缩短会话后重试。")
    return result


def zip_conversation(text: str) -> bytes:
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        raise ValueError("审计快照超过 10 MB 上限。")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("conversation.txt", encoded)
    return output.getvalue()
