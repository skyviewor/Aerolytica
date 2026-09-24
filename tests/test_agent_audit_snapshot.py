from __future__ import annotations

import io
import zipfile

from cryptography.fernet import Fernet

from aero.agent import session as session_module
from aero.agent.audit_snapshot import render_conversation, zip_conversation
from aero.agent.session import SessionManager, SessionMeta
from aero.core.types import Message, ToolCall


def test_audit_export_contains_persisted_tool_context_but_masks_secrets(tmp_path, monkeypatch):
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr(session_module, "_get_fernet", lambda: cipher)
    manager = SessionManager(tmp_path)
    manager.save("test-session", [
        Message(role="user", content="你好"),
        Message(role="assistant", content="调用工具", tool_calls=[
            ToolCall(id="call-1", name="search", arguments={"query": "天气", "api_key": "verysecretkey"})
        ]),
        Message(role="tool", content="结果：晴天", tool_call_id="call-1"),
    ], SessionMeta(id="test-session", name="测试智能体", model="test-model"))

    text = render_conversation(manager, "test-session")
    assert "测试智能体" in text
    assert "call-1" in text
    assert "结果：晴天" in text
    assert "verysecretkey" not in text
    assert "[API_KEY_REDACTED]" in text

    with zipfile.ZipFile(io.BytesIO(zip_conversation(text))) as archive:
        assert archive.namelist() == ["conversation.txt"]
        assert archive.read("conversation.txt").decode() == text
