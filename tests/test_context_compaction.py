from __future__ import annotations

from types import SimpleNamespace

import pytest

from aero.application import local_session
from aero.application.context_compaction import compacted_messages, summary_prompt_for
from aero.core.types import Message


def test_shared_compaction_keeps_summary_and_tool_names():
    messages = [Message(role="system", content="instructions"),
                Message(role="user", content="do this"),
                Message(role="assistant", content="result")]
    assert "[user]: do this" in summary_prompt_for(messages)
    compacted = compacted_messages(messages[0], "state")
    assert len(compacted) == 3
    assert compacted[1].content == "[compact_summary]\nstate"


@pytest.mark.asyncio
async def test_local_session_auto_compacts_known_context_before_request(monkeypatch):
    session = local_session.LocalSession.__new__(local_session.LocalSession)
    session.config = SimpleNamespace(llm=SimpleNamespace(
        model="known", provider="official", base_url="https://example.test",
        active_api_key=lambda: "test",
    ))
    session.agent = SimpleNamespace(messages=[
        Message(role="system", content="system"),
        Message(role="user", content="A" * 200),
        Message(role="assistant", content="B" * 200),
        Message(role="user", content="C" * 200),
    ])
    session.id = "session_1"
    saved = []
    session._save = lambda: saved.append(list(session.agent.messages))
    session.session_manager = SimpleNamespace(export_portable_context=lambda _id: {
        "schema": 1, "meta": {},
        "messages": [{"role": message.role, "content": message.content}
                     for message in session.agent.messages],
    })
    events = []
    async def on_compaction(before, after):
        events.append((before, after))
    session.on_compaction = on_compaction
    monkeypatch.setattr(local_session, "context_window_for", lambda _model: 100)
    class FakeClient:
        def __init__(self, _config):
            pass
        async def chat(self, _messages):
            return "reduced state"
        async def close(self):
            pass
    monkeypatch.setattr(local_session, "LLMClient", FakeClient)

    assert await session.compact_if_needed()
    assert len(saved) == 2
    assert events[0][0]["messages"][1]["content"] == "A" * 200
    assert events[0][1]["messages"][1]["content"] == "[compact_summary]\nreduced state"


@pytest.mark.asyncio
async def test_unknown_context_limit_does_not_auto_compact(monkeypatch):
    session = local_session.LocalSession.__new__(local_session.LocalSession)
    session.config = SimpleNamespace(llm=SimpleNamespace(model="unknown"))
    monkeypatch.setattr(local_session, "context_window_for", lambda _model: None)
    assert not await session.compact_if_needed()
