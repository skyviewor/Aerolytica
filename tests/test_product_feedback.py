"""Official feedback is consented, minimal, idempotent, and model-independent."""

import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from aero.agent.loop import AgentLoop, _direct_tool_response
from aero.agent.system_prompt import build_system_prompt
from aero.application.turn_context import current_turn_id, use_turn_id
from aero.core.config import AeroConfig
from aero.core.official_account import OfficialAccountSession, OfficialSessionData
from aero.core.types import ToolCall
from aero.data.modes import is_tool_allowed
from aero.toolbox.registry import get_registry
from aero.toolbox.tools import feedback
from aero.version import get_version

RECEIPT = {"received": True, "feedback_id": "fb_" + "a" * 32}
ARGS = {"title": "改善体验", "content": "希望操作更方便。", "category": "experience"}


@pytest.fixture
def account(monkeypatch):
    import aero.core.official_account as official

    stored = OfficialSessionData(
        access_token="test-access-token",
        refresh_token="test-refresh",
        access_expires_at=time.time() + 3600,
        user_id="alice",
    )
    monkeypatch.setattr(official, "load_official_session", lambda: stored)
    return official


async def invoke(monkeypatch, account, handler, *, turn="feedback-turn", args=None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        session = OfficialAccountSession(base_url="https://official.test", client=http)
        monkeypatch.setattr(feedback, "OfficialAccountSession", lambda: session)
        with use_turn_id(turn):
            return await feedback.submit_product_feedback(**(args or ARGS))


async def test_minimal_redacted_payload_and_real_version(monkeypatch, account):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(201, json={**RECEIPT, "private": "do not return this"})

    result = await invoke(
        monkeypatch,
        account,
        handler,
        args={
            **ARGS,
            "content": "API sk-private-key-12345678 me@example.com "
            "Bearer abcdefghijklmnop /Users/alice/private/report.md "
            r"C:\Users\alice\work\report.md /home/bob/private.txt ~/papers/topic.md",
        },
    )
    assert result["received"] is True and result["status"] == "success"
    assert "不保证逐条回复" in result["message"]
    assert len(calls) == 1
    request = calls[0]
    body = json.loads(request.content)
    assert request.url.path == "/v1/feedback" and request.method == "POST"
    assert request.headers["Authorization"] == "Bearer test-access-token"
    assert set(body) == {"title", "content", "category", "request_id", "client_version", "os_type"}
    assert body["client_version"] == get_version()
    assert body["os_type"] in {"Linux", "Darwin", "Windows", "unknown"}
    for private in (
        "sk-private",
        "me@example",
        "abcdefghijklmnop",
        "alice",
        "bob",
        "report.md",
        "topic.md",
    ):
        assert private not in body["content"]
    assert "private" not in str(result)


async def test_identity_is_stable_within_message_but_not_between_messages(monkeypatch, account):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json=RECEIPT)

    await invoke(monkeypatch, account, handler)
    await invoke(monkeypatch, account, handler, args={**ARGS, "title": " 改善体验 "})
    await invoke(monkeypatch, account, handler, turn="new-turn")
    await invoke(monkeypatch, account, handler, args={**ARGS, "content": "不同的建议"})
    assert bodies[0] == bodies[1]
    assert len({b["request_id"] for b in bodies}) == 3


def test_common_json_credentials_keys_and_private_key_blocks_are_redacted():
    value = feedback._feedback_text(
        '\n{"api_key": "privatevalue", "refresh_token": "myrefreshvalue"}\n'
        "ghp_abcdefghijklmnop rly_abcdefghijklmnop "
        "eyJabcdefg.abcdefg.signature\n"
        "-----BEGIN PRIVATE KEY-----\nprivatekeycontent\n-----END PRIVATE KEY-----"
    )
    for secret in (
        "privatevalue",
        "myrefreshvalue",
        "abcdefghijklmnop",
        "eyJabcdefg",
        "privatekeycontent",
    ):
        assert secret not in value


async def test_unauthorized_refresh_reuses_the_same_request_body(monkeypatch, account):
    bodies = []

    def handler(request):
        bodies.append(request.content)
        return httpx.Response(401 if len(bodies) == 1 else 201, json=RECEIPT)

    monkeypatch.setattr(
        OfficialAccountSession, "access_token", AsyncMock(return_value="rotating-token")
    )
    result = await invoke(monkeypatch, account, handler)
    assert result["status"] == "success" and len(bodies) == 2 and bodies[0] == bodies[1]


@pytest.mark.parametrize(
    "code,payload,status",
    [
        (404, {}, "error"),
        (409, {}, "error"),
        (422, {}, "error"),
        (429, {}, "error"),
        (500, {}, "unknown"),
        (201, {}, "unknown"),
        (200, {"received": False}, "unknown"),
        (201, {**RECEIPT, "feedback_id": "<script>unsafe"}, "unknown"),
    ],
)
async def test_failures_never_claim_success_or_retry(monkeypatch, account, code, payload, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code, json=payload)

    result = await invoke(monkeypatch, account, handler)
    assert result["status"] == status and result.get("received") is not True
    assert len(calls) == 1 and "feedback_id" not in result


async def test_network_uncertainty_is_not_automatic_replay(monkeypatch, account):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private internal URL")

    result = await invoke(monkeypatch, account, handler)
    assert result["status"] == "unknown" and len(calls) == 1
    assert "不要自动重复投递" in result["message"] and "private" not in str(result)


async def test_login_required_without_using_provider_credentials(monkeypatch, account):
    monkeypatch.setattr(account, "load_official_session", OfficialSessionData)

    def handler(request):
        pytest.fail("Logged-out suggestions must not contact a remote service")

    result = await invoke(monkeypatch, account, handler)
    assert result["status"] == "error" and "/login" in result["message"]


@pytest.mark.parametrize(
    "args",
    [{**ARGS, "title": " "}, {**ARGS, "content": "x" * 10001}, {**ARGS, "category": "support"}],
)
async def test_validation_before_network(monkeypatch, args):
    factory = AsyncMock()
    monkeypatch.setattr(feedback, "OfficialAccountSession", factory)
    with use_turn_id("turn"):
        result = await feedback.submit_product_feedback(**args)
    assert result["status"] == "error"
    factory.assert_not_called()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_consent_prompts_and_all_modes(language):
    import aero.toolbox.builtin_tools  # noqa: F401

    prompt = build_system_prompt(AeroConfig.create_default(), language)
    spec = get_registry().get("submit_product_feedback")
    assert not spec.requires_confirmation
    assert "先询问一次" in spec.description
    assert "submit_product_feedback" in prompt
    assert "prepare_issue_link" in prompt
    for mode in ("execute", "plan", "qa"):
        assert is_tool_allowed(spec.name, mode)
    if language == "zh":
        assert "未获同意不得调用" in prompt and "不保证逐条回复" in prompt
    else:
        assert "do not submit without consent" in prompt and "individual replies" in prompt


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "result",
    [
        {"status": "success", **RECEIPT},
        {"status": "unknown", "message": "提交结果不明确，不要自动重复投递。"},
        {"status": "error", "message": "请先使用 /login 登录。"},
    ],
)
async def test_receipt_does_not_trigger_another_model_round(monkeypatch, stream, result):
    config = AeroConfig.create_default()
    agent = AgentLoop(config)
    spec = get_registry().get("submit_product_feedback")
    monkeypatch.setattr(spec, "function", AsyncMock(return_value=result))
    agent.llm = AsyncMock()
    call = ToolCall(id="feedback-call", name=spec.name, arguments=ARGS)
    if stream:
        events = [event async for event in agent._handle_tool_calls_stream([call], "")]
        message = "".join(event.content for event in events if event.type == "text")
    else:
        message = await agent._handle_tool_calls([call], "")
    assert message == _direct_tool_response(spec.name, result)
    agent.llm.chat.assert_not_called()
    agent.llm.chat_with_tools_stream.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
async def test_entrypoints_supply_and_preserve_turn_identity(monkeypatch, stream):
    agent = AgentLoop(AeroConfig.create_default())
    observed = []

    async def plain(text):
        observed.append(current_turn_id())
        return text

    async def streaming(text):
        observed.append(current_turn_id())
        yield text

    monkeypatch.setattr(agent, "_run_unlocked", plain)
    monkeypatch.setattr(agent, "_run_stream_unlocked", streaming)

    async def run():
        if stream:
            return [x async for x in agent.run_stream("submit")]
        return await agent.run("submit")

    await run()
    await run()
    with use_turn_id("existing-turn"):
        await run()
    assert observed[0] and observed[0] != observed[1] and observed[2] == "existing-turn"
