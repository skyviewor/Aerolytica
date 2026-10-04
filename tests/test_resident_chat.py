import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from textual.widgets import Static

from aero.cli import resident_chat
from aero.core.official_account import OfficialLoginRequiredError


@pytest.fixture(autouse=True)
def isolated_ui_preferences(monkeypatch):
    monkeypatch.setattr(resident_chat, "_load_saved_theme", lambda: "")


@pytest.mark.parametrize("selector", ["my_agent", "agent_original"])
def test_parse_selector_and_preserve_mouse_flags(selector):
    assert resident_chat.chat_agent_args(["--no-mouse", "--agent", selector]) == (
        selector,
        ["--no-mouse"],
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--agent"],
        ["--agent", "--mouse"],
        ["--agent", "a", "--agent", "b"],
        ["--agent", "a", "--service"],
        ["--agent", "a", "--attach"],
        ["--agent", "a", "--continue"],
        ["--agent", "a", "-c"],
    ],
)
def test_incompatible_or_missing_selector_is_rejected(args):
    with pytest.raises(ValueError):
        resident_chat.chat_agent_args(args)


def make_conversation(monkeypatch, handler):
    registry = Mock()
    registry.resolve.return_value = SimpleNamespace(agent_id="original", name="my_agent")
    monkeypatch.setattr(resident_chat, "RemoteAgentRegistry", lambda: registry)
    client = httpx.AsyncClient(
        base_url="https://isolated.invalid",
        transport=httpx.MockTransport(handler),
    )
    account = SimpleNamespace(
        base_url="https://isolated.invalid",
        request=client.request,
        access_token=AsyncMock(return_value="account-token"),
        close=client.aclose,
    )
    return resident_chat.ResidentConversation("my_agent", account=account, stream_client=client)


async def test_original_history_pagination_and_sse_cursor(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        path = request.url.path
        if path == "/v1/agents":
            return httpx.Response(
                200,
                json={
                    "agents": [
                        {"agent_id": "original", "agent_type": "resident", "status": "online"}
                    ],
                    "event_cursor": "initial",
                },
            )
        if path.endswith("/messages"):
            if request.url.params.get("before"):
                return httpx.Response(200, json={"messages": [{"message_id": "old"}]})
            return httpx.Response(
                200, json={"messages": [{"message_id": "new"}], "next_cursor": "old_page"}
            )
        if path.endswith("/approvals"):
            return httpx.Response(200, json={"approvals": []})
        assert path == "/v1/agents/events"
        assert request.headers["authorization"] == "Bearer account-token"
        assert request.headers["last-event-id"] == "initial"
        return httpx.Response(
            200,
            text=": keepalive\n\nid: next\nevent: agent.status\n"
            'data: {"agent_id":"original","status":"busy"}\n\n',
        )

    conversation = make_conversation(monkeypatch, handler)
    messages, approvals = await conversation.snapshot(full_history=True)
    assert [row["message_id"] for row in messages] == ["old", "new"]
    assert approvals == []
    events = [event async for event in conversation.events()]
    assert events == [("agent.status", {"agent_id": "original", "status": "busy"})]
    assert conversation.cursor == "next"
    await conversation.close()
    assert all(request.method == "GET" for request in calls)


class FakeConversation:
    agent_id = "original"
    name = "my_agent"

    def __init__(self):
        self.agent = {"status": "online"}
        self.close = AsyncMock()
        self.request = AsyncMock(
            return_value={
                "message": {
                    "message_id": "sent",
                    "sender": "user",
                    "content": "继续原任务",
                }
            }
        )
        self.snapshot = AsyncMock(
            return_value=(
                [
                    {"message_id": "old", "sender": "agent", "content": "原对话"},
                ],
                [],
            )
        )

    async def events(self):
        await asyncio.Event().wait()
        yield "unused", {}


async def test_real_tui_reads_sends_updates_same_conversation_and_closes_only_view():
    conversation = FakeConversation()
    app = resident_chat.ResidentChatApp("my_agent", conversation=conversation)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "old" in app._rows
        app.query_one(resident_chat.ResidentChatInput).load_text("继续原任务")
        await pilot.press("enter")
        await pilot.pause()
        call = conversation.request.await_args
        assert call.args == ("POST", "messages")
        assert call.kwargs["json"]["content"] == "继续原任务"
        app._event(
            "agent.message",
            {"agent_id": "other", "message": {"message_id": "wrong", "content": "别人的对话"}},
        )
        assert "wrong" not in app._rows
        app._event(
            "agent.message",
            {
                "agent_id": "original",
                "message": {"message_id": "answer", "sender": "agent", "content": "后台回复"},
            },
        )
        app._event(
            "agent.message_updated",
            {
                "agent_id": "original",
                "message": {"message_id": "answer", "sender": "agent", "content": "完整回复"},
            },
        )
        assert "完整回复" in app._rows["answer"]._markdown
        app._status("重连中，在线状态待确认")
        assert "在线待命" not in str(app.query_one("#footer-status", Static).render())
        for percent in range(100):
            app._message(
                {
                    "message_id": f"progress_{percent}",
                    "message_type": "progress",
                    "content": f"下载进度#1 {percent}%",
                    "metadata": {"in_reply_to": "turn"},
                }
            )
        assert len([key for key in app._rows if key.startswith("progress:")]) == 1
        await pilot.press("ctrl+q")
    conversation.close.assert_awaited_once()
    assert conversation.request.await_count == 1
    conversation.snapshot.assert_awaited_once_with(full_history=True)


async def test_send_failure_never_automatically_resubmits():
    conversation = FakeConversation()
    conversation.request.side_effect = httpx.ConnectError("isolated disconnect")
    app = resident_chat.ResidentChatApp("my_agent", conversation=conversation)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._send("继续原任务")
        assert conversation.request.await_count == 1
        assert "不会自动重发" in str(app._rows["action-error"].render())


async def test_expired_login_shows_login_command_without_retry_loop():
    conversation = FakeConversation()
    conversation.snapshot.side_effect = OfficialLoginRequiredError("expired")
    app = resident_chat.ResidentChatApp("my_agent", conversation=conversation)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app._booted
        assert "aero login" in str(app._rows["connection-error"].render())
        conversation.snapshot.assert_awaited_once()


def test_cli_dispatches_viewer_without_creating_normal_chat_or_daemon(monkeypatch, tmp_path):
    import sys

    from aero.cli import main as cli
    from aero.core.daemon import DaemonManager

    monkeypatch.setattr(sys, "argv", ["aero", "chat", "--agent", "my_agent", "--no-mouse"])
    monkeypatch.setattr(cli, "configure_debug_logging", lambda: tmp_path / "debug.log")
    monkeypatch.setattr(cli, "configure_logging", Mock())
    monkeypatch.setattr(cli, "debug_log", Mock())
    normal_chat = Mock(side_effect=AssertionError("must not start second executor"))
    daemon = Mock(side_effect=AssertionError("must not start daemon"))
    monkeypatch.setattr(cli, "AeroApp", normal_chat)
    monkeypatch.setattr(DaemonManager, "start", daemon)
    viewer = Mock()
    factory = Mock(return_value=viewer)
    monkeypatch.setattr(resident_chat, "ResidentChatApp", factory)
    cli.main()
    factory.assert_called_once_with("my_agent")
    viewer.run.assert_called_once_with(mouse=False)
    normal_chat.assert_not_called()
    daemon.assert_not_called()


async def test_pending_approval_resolved_through_original_agent():
    conversation = FakeConversation()
    conversation.request.return_value = {
        "approval": {"approval_id": "approval_original", "status": "approved"}
    }
    app = resident_chat.ResidentChatApp("my_agent", conversation=conversation)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._approval(
            {
                "approval_id": "approval_original",
                "status": "pending",
                "payload": {"tool_name": "run_shell", "command": "pwd"},
            }
        )
        await app._send("/approve approval_original")
        conversation.request.assert_awaited_once_with(
            "POST",
            "approvals/approval_original/resolve",
            json={"decision": "approved"},
        )


async def test_normal_chat_widgets_render_markdown_and_multiline_editor():
    from textual.widgets import Header

    conversation = FakeConversation()
    app = resident_chat.ResidentChatApp("my_agent", conversation=conversation)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert app.CSS == resident_chat.AeroApp.CSS
        assert not app.query(Header)
        editor = app.query_one(resident_chat.ChatTextArea)
        assert editor.has_focus
        app._message(
            {
                "message_id": "rich",
                "sender": "agent",
                "content": "## 出图说明\n\n**双面板**\n\n| 日期 | 温度 |\n| --- | --- |\n"
                "| 09-01 | 20 |\n\n```python\nprint('ok')\n```",
            }
        )
        await pilot.pause()
        body = app._rows["rich"]
        assert isinstance(body, resident_chat.ChatMarkdown)
        assert body.query("MarkdownH2")
        assert body.query("MarkdownTable")
        assert body.query("MarkdownFence")
        await pilot.press("a", "shift+enter", "b")
        assert editor.text == "a\nb"
        conversation.request.assert_not_awaited()
        await pilot.press("enter")
        await pilot.pause()
        assert conversation.request.await_args.kwargs["json"]["content"] == "a\nb"
        assert not editor.text


async def test_mode_theme_copy_and_editor_escape_use_normal_ui(monkeypatch):
    monkeypatch.setattr(resident_chat, "_load_saved_theme", lambda: "textual-light")
    conversation = FakeConversation()
    app = resident_chat.ResidentChatApp("my_agent", conversation=conversation)
    clipboard = Mock()
    monkeypatch.setattr(app, "copy_to_clipboard", clipboard)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "textual-light"
        await pilot.press("tab")
        assert app._mode == "qa"
        assert app.query_one("#input-box").has_class("mode-qa")
        await pilot.press("ctrl+y")
        clipboard.assert_called_once_with("原对话")
        editor = app.query_one(resident_chat.ResidentChatInput)
        editor.load_text("尚未发送")
        await pilot.press("ctrl+c")
        assert not editor.text
        await pilot.press("escape")
        assert app.query_one("#chat-area").has_focus
        conversation.request.assert_not_awaited()


async def test_slash_menu_filters_selects_mode_and_closes_on_escape():
    conversation = FakeConversation()
    app = resident_chat.ResidentChatApp("my_agent", conversation=conversation)
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(resident_chat.ResidentChatInput)
        await pilot.press("/")
        await pilot.pause()
        assert app._cmd_list.styles.display == "block"
        assert app.query_one("#command-row").styles.display == "block"
        assert {command for command, _ in app._filtered_commands} == {
            command for command, _ in app._commands_list
        }
        await pilot.press("down")
        assert app._cmd_list.index == 1
        await pilot.press("escape")
        assert app._cmd_list.styles.display == "none"
        editor.load_text("/mode qa")
        editor.focus()
        await pilot.pause()
        assert app._filtered_commands == [("/mode qa", "问答模式")]
        await pilot.press("enter")
        await pilot.pause()
        assert app._mode == "qa"
        assert not editor.text
        conversation.request.assert_not_awaited()


async def test_bare_slash_model_command_opens_normal_picker(monkeypatch):
    catalog = Mock()
    catalog.models = AsyncMock(return_value=[SimpleNamespace(id="auto", display_name="自动")])
    monkeypatch.setattr(resident_chat, "OfficialModelCatalog", lambda: catalog)
    conversation = FakeConversation()
    app = resident_chat.ResidentChatApp("my_agent", conversation=conversation)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(resident_chat.ResidentChatInput).load_text("/model")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, resident_chat.SelectScreen)
        catalog.models.assert_awaited_once()
        await pilot.press("escape")
        conversation.request.assert_not_awaited()
