"""Frozen account policy and trusted execution metadata."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from aero.agent.llm_client import LLMClient, LLMConfig
from aero.agent.loop import AgentLoop
from aero.agent.official_vision_client import OfficialVisionClient
from aero.application.remote_command import command_settings, use_remote_command
from aero.application.turn_context import current_turn_id, use_turn_id
from aero.core import agent_preferences
from aero.core.agent_preferences import AgentPreferences
from aero.core.config import VisionConfig
from aero.core.types import Message


@pytest.mark.asyncio
async def test_tool_vision_has_separate_purpose_and_same_question_identity(monkeypatch):
    session = SimpleNamespace(
        request_to=AsyncMock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "red"}}]})
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr("aero.agent.official_vision_client.OfficialAccountSession", lambda: session)
    client = OfficialVisionClient(VisionConfig(model="account-vision"))
    with use_turn_id("command-stable"):
        assert await client.analyze([], "Identify color") == "red"
    kwargs = session.request_to.call_args.kwargs
    assert kwargs["headers"]["x-relay-question-purpose"] == "vision"
    assert kwargs["headers"]["x-relay-turn-id"] == "command-stable"
    assert kwargs["headers"]["Idempotency-Key"]
    assert kwargs["json"]["relay_turn_id"] == "command-stable"
    assert kwargs["json"]["model"] == "account-vision"
    assert "relay_purpose" not in kwargs["json"]
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 999, None])
async def test_preferences_fetch_does_not_inject_prompt(monkeypatch, limit):
    session = SimpleNamespace(
        request=AsyncMock(
            return_value=httpx.Response(
                200,
                request=httpx.Request("GET", "https://relay.test/v1/agents/preferences"),
                json={"custom_prompt": "PRIVATE PROMPT", "max_rounds": limit, "version": 7},
            )
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(agent_preferences, "OfficialAccountSession", lambda: session)
    prefs = await agent_preferences.fetch_agent_preferences()
    assert prefs == AgentPreferences(limit, 7)
    assert not hasattr(prefs, "custom_prompt")
    session.request.assert_awaited_once_with("GET", "/v1/agents/preferences")
    session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 999, None])
async def test_begin_question_uses_atomic_turn_endpoint(monkeypatch, limit):
    session = SimpleNamespace(
        request=AsyncMock(
            return_value=httpx.Response(
                200,
                request=httpx.Request("POST", "https://relay.test/v1/agents/preferences/questions"),
                json={"turn_id": "stable", "max_rounds": limit, "version": 3},
            )
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(agent_preferences, "OfficialAccountSession", lambda: session)
    assert await agent_preferences.begin_agent_question("stable") == AgentPreferences(limit, 3)
    session.request.assert_awaited_once_with(
        "POST", "/v1/agents/preferences/questions", json={"turn_id": "stable"}
    )
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_turn_freezes_policy_and_tool_cannot_raise_ceiling(monkeypatch):
    fetch = AsyncMock(return_value=AgentPreferences(2, 1))
    monkeypatch.setattr(agent_preferences, "begin_agent_question", fetch)
    agent = AgentLoop.__new__(AgentLoop)
    agent.config = SimpleNamespace(llm=SimpleNamespace(provider="official"))
    agent.llm = SimpleNamespace()
    with use_turn_id("command-stable"):
        await agent._prepare_account_turn()
        await agent._prepare_account_turn()  # Prefreeze before compact, then the same main loop.
    assert agent.llm.relay_turn_id == "command-stable"
    assert agent._account_preferences == AgentPreferences(2, 1)
    fetch.return_value = AgentPreferences(None, 2)
    agent._begin_main_round()
    agent.max_tool_rounds = 99999  # A model tool changes only local configuration.
    agent._begin_main_round()
    with pytest.raises(RuntimeError, match="账户模型轮次上限"):
        agent._begin_main_round()
    fetch.assert_awaited_once()
    await agent._prepare_account_turn()
    for _ in range(1001):
        agent._begin_main_round()


@pytest.mark.parametrize("purpose", ["main", "title", "compact"])
@pytest.mark.asyncio
async def test_official_wire_purpose_and_turn_identity(purpose):
    client = LLMClient(LLMConfig(provider="official"))
    client._official_session = SimpleNamespace(
        access_token=AsyncMock(return_value="account"), close=AsyncMock()
    )
    client.relay_turn_id = "command-123"
    client.relay_purpose = purpose
    try:
        headers = await client._request_headers()
        body = client._request_body([Message(role="user", content="hello")])
        assert headers["Authorization"] == "Bearer account"
        assert headers["x-relay-question-purpose"] == purpose
        assert "relay_purpose" not in body
        assert headers["x-relay-turn-id"] == body["relay_turn_id"] == "command-123"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "model,expected", [("auto", "auto"), ("default", "auto"), ("manual-model", "manual-model")]
)
def test_command_settings_restore_on_failure(model, expected):
    agent = SimpleNamespace(
        config=SimpleNamespace(llm=SimpleNamespace(model="original"), mode="execute"),
        llm=SimpleNamespace(config=SimpleNamespace(model="original")),
    )
    with pytest.raises(RuntimeError), use_remote_command({"model": model, "mode": "plan"}):
        with command_settings(agent):
            assert agent.config.llm.model == agent.llm.config.model == expected
            assert agent.config.mode == "plan"
            raise RuntimeError("failed command")
    assert agent.config.llm.model == agent.llm.config.model == "original"
    assert agent.config.mode == "execute"


def test_turn_id_validation_and_reset():
    with use_turn_id("stable"):
        assert current_turn_id() == "stable"
    assert current_turn_id() == ""
    with pytest.raises(ValueError):
        with use_turn_id("x" * 65):
            pass


def test_hidden_cli_commands_do_not_call_library(monkeypatch, capsys):
    from aero.cli import agent_runner

    takeover = AsyncMock()
    monkeypatch.setattr(agent_runner, "_takeover", takeover)
    assert agent_runner.run_agent_cli(["clone", "agent"]) == 1
    assert agent_runner.run_agent_cli(["context-sync", "agent", "off"]) == 1
    takeover.assert_not_called()
    agent_runner.run_agent_cli(["help"])
    help_text = capsys.readouterr().out.split("aero agent register")[-1]
    assert "aero agent clone" not in help_text
    assert "aero agent context-sync" not in help_text


@pytest.mark.asyncio
async def test_remote_frozen_snapshot_skips_current_account_fetch(monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(agent_preferences, "begin_agent_question", fetch)
    agent = AgentLoop.__new__(AgentLoop)
    agent.config = SimpleNamespace(llm=SimpleNamespace(provider="official"))
    agent.llm = SimpleNamespace()
    with use_remote_command({"max_rounds": 1, "preference_version": 4}):
        await agent._prepare_account_turn()
    assert agent._account_preferences == AgentPreferences(1, 4)
    agent._begin_main_round()
    with pytest.raises(RuntimeError):
        agent._begin_main_round()
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_main_limit_counts_initial_request_not_tools(monkeypatch):
    from aero.agent.llm_client import StreamEvent
    from aero.core.config import AeroConfig
    from aero.core.types import ToolCall

    monkeypatch.setattr(
        agent_preferences, "begin_agent_question", AsyncMock(return_value=AgentPreferences(1, 1))
    )
    config = AeroConfig()
    config.llm.provider = "official"
    agent = AgentLoop(config)
    agent._tools_for_turn = AsyncMock(return_value=[])
    requests = []

    async def model(messages, tools):
        requests.append(agent.llm.relay_turn_id)
        yield StreamEvent(type="tool_call", tool_call=ToolCall("tool", "unknown", {}))
        yield StreamEvent(type="done")

    agent.llm.chat_with_tools_stream = model
    try:
        with use_turn_id("durable-turn"):
            events = [event async for event in agent.run_stream("hello")]
        assert requests == ["durable-turn"]
        assert agent.messages[1].message_id == "durable-turn"
        assert any("账户模型轮次上限" in event.content for event in events)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_resident_command_preserves_turn_and_options(tmp_path):
    from aero.application.cloud_runtime import CloudAgentRuntime
    from aero.application.remote_command import current_command

    received = []

    def start(prompt, *, origin):
        received.append((prompt, origin, current_turn_id(), current_command()))
        return "run"

    async def events(_run):
        if False:
            yield

    session = SimpleNamespace(
        id="session",
        project_dir=tmp_path,
        start_run=start,
        events=events,
        run_status=lambda _: {"state": "completed"},
    )
    transport = SimpleNamespace(agent_id="agent", request=AsyncMock(return_value={}))
    runtime = CloudAgentRuntime(
        session,
        transport,
        device_id="device",
        project_id="project",
        account_id="user",
        account_identity=lambda: "user",
        state_dir=tmp_path / "state",
    )
    runtime._assert_lease = lambda: None
    value = {
        "command_id": "stable-command",
        "content": "hello",
        "model": "model-id",
        "mode": "qa",
        "attachments": [],
    }
    await runtime._execute(value)
    assert received == [("hello", "web", "stable-command", value)]
    assert current_turn_id() == ""
