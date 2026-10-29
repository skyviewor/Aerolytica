"""Relay alone chooses models; clients preserve identity and observe metadata."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from aero.agent.llm_client import LLMClient, LLMConfig
from aero.agent.loop import AgentLoop
from aero.application.cloud_runtime import CloudAgentRuntime
from aero.application.local_session import LocalSession
from aero.application.turn_context import use_turn_id
from aero.core.config import AeroConfig
from aero.core.types import Message
from aero.data.plans import use_session_id


@pytest.mark.parametrize("provider", ["official", "openai"])
def test_identity_fields_only_reach_relay(provider):
    client = LLMClient(LLMConfig(provider=provider, model="auto"))
    client.relay_conversation_id = "saved-session"
    client.relay_turn_id = "turn"
    body = client._request_body([Message(role="user", content="hi")])
    headers = client._headers()
    if provider == "official":
        assert body["relay_conversation_id"] == "saved-session"
        assert headers["x-relay-conversation-id"] == "saved-session"
    else:
        assert "relay_conversation_id" not in body
        assert "x-relay-conversation-id" not in headers


@pytest.mark.parametrize("mode", ["chat", "stream", "tools"])
async def test_resolved_model_observed_without_overriding_auto(monkeypatch, mode):
    wire = []

    def handler(request):
        wire.append(request)
        headers = {
            "X-Relay-Logical-Model": "kimi-k3",
            "X-Relay-Routing-Reason": "feedback_rematch",
            "X-Relay-Routing-Policy-Version": "jev-routing-v1",
        }
        if json.loads(request.content).get("stream"):
            return httpx.Response(
                200,
                headers=headers,
                text=('data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'),
                request=request,
            )
        return httpx.Response(
            200,
            headers=headers,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=request,
        )

    client = LLMClient(LLMConfig(provider="official", model="auto"))
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.relay_conversation_id = "saved-session"
    client.relay_turn_id = "turn"

    async def headers(**kwargs):
        return client._headers()

    monkeypatch.setattr(client, "_request_headers", headers)
    messages = [Message(role="user", content="hi")]
    if mode == "chat":
        assert await client.chat(messages) == "ok"
    elif mode == "stream":
        assert "".join([event.content async for event in client.chat_stream(messages)]) == "ok"
    else:
        events = [event async for event in client.chat_with_tools_stream(messages, [])]
        assert any(event.content == "ok" for event in events)
    assert client.last_resolved_model == "kimi-k3"
    assert client.last_routing_reason == "feedback_rematch"
    assert client.last_routing_policy_version == "jev-routing-v1"
    assert client.config.model == "auto"
    assert wire[0].headers["x-relay-conversation-id"] == "saved-session"
    assert json.loads(wire[0].content)["relay_conversation_id"] == "saved-session"
    await client.close()


async def test_conversation_restore_new_and_hosted_identity(monkeypatch, tmp_path):
    from aero.core.agent_preferences import AgentPreferences

    async def preferences(turn_id):
        return AgentPreferences()

    monkeypatch.setattr("aero.core.agent_preferences.begin_agent_question", preferences)
    config = AeroConfig()
    config.llm.provider = "openai"
    session = LocalSession(tmp_path, config, session_id="restored-session")
    assert session.agent.llm.relay_conversation_id == "restored-session"
    agent = session.agent
    with use_session_id("restored-session"), use_turn_id("one"):
        await agent._prepare_account_turn()
        assert agent.llm.relay_conversation_id == "restored-session"
        assert agent.llm.relay_turn_id == "one"
    with use_session_id("restored-session"), use_turn_id("two"):
        await agent._prepare_account_turn()
        assert agent.llm.relay_conversation_id == "restored-session"
        assert agent.llm.relay_turn_id == "two"
    with use_session_id("new-session"), use_turn_id("three"):
        await agent._prepare_account_turn()
        assert agent.llm.relay_conversation_id == "new-session"
    await agent.llm.close()


def test_usage_records_actual_model():
    recorded = []
    stub = SimpleNamespace(
        llm=SimpleNamespace(last_resolved_model="kimi-k3", last_request_started_at=None),
        config=SimpleNamespace(llm=SimpleNamespace(model="auto", provider="official")),
        tracker=SimpleNamespace(add_llm=lambda usage, model, **kwargs: recorded.append(model)),
    )
    AgentLoop._record_llm_usage(stub, {"prompt_tokens": 10})
    assert recorded == ["kimi-k3"]


def test_hosted_foreground_reply_keeps_actual_model(monkeypatch, tmp_path):
    runtime = CloudAgentRuntime.__new__(CloudAgentRuntime)
    runtime.session = SimpleNamespace(
        id="session",
        agent=SimpleNamespace(llm=SimpleNamespace(last_resolved_model="kimi-k3")),
        run_status=lambda _: {"state": "completed"},
        session_manager=SimpleNamespace(export_portable_context=lambda _: None),
        messages_view=lambda: [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "done"},
        ],
    )
    runtime.images = SimpleNamespace(describe=lambda *_: [])
    runtime._conversation_events = []
    runtime._conversation_events_path = tmp_path / "events.json"
    monkeypatch.setattr("aero.application.cloud_runtime.private_write", Mock())
    runtime._queue_foreground_messages("run")
    assert runtime._conversation_events[0]["resolved_model"] == "kimi-k3"
    assert "resolved_model" not in runtime._conversation_events[1]
    runtime.session.agent.llm.last_resolved_model = "[red]unsafe"
    assert runtime._resolved_model_metadata() == {}
