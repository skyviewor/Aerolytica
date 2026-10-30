"""No live browser, identity service or existing account files are used."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Vertical
from textual.widgets import OptionList, Static

from aero.cli.device_login import OfficialLoginApp, OfficialLoginScreen, authorization_status
from aero.core.device_login import DeviceLoginController, login_recommendation
from aero.core.official_account import (
    OfficialAccountSession,
    OfficialSessionData,
    load_official_session,
    save_official_session,
)
from aero.server.cloud import CloudServices, cloud_router
from aero.version import get_version

START = {
    "device_code": "private-" + "x" * 40,
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://web.test/activate",
    "interval": 5,
    "expires_in": 600,
}
TOKENS = {
    "user_id": "new-user",
    "email": "new@example.test",
    "access_token": "new-access",
    "refresh_token": "new-refresh",
    "expires_in": 600,
    "refresh_expires_in": 86400,
}


@pytest.fixture(autouse=True)
def isolated_account(tmp_path, monkeypatch):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    for name in (
        "SSH_CONNECTION",
        "SSH_CLIENT",
        "SSH_TTY",
        "CI",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "TERM_PROGRAM",
        "SESSIONNAME",
    ):
        monkeypatch.delenv(name, raising=False)
    save_official_session(
        OfficialSessionData(
            user_id="existing-user",
            access_token="old-access",
            refresh_token="old-refresh",
            access_expires_at=time.time() + 600,
            refresh_expires_at=time.time() + 86400,
        )
    )


@pytest.mark.parametrize(
    "system,environment,tty,expected",
    [
        ("Darwin", {"TERM_PROGRAM": "iTerm.app"}, True, "browser"),
        ("Darwin", {"TERM_PROGRAM": "iTerm.app", "SSH_TTY": "/dev/pts/0"}, True, "manual"),
        ("Darwin", {}, True, "manual"),
        ("Linux", {}, True, "manual"),
        ("Linux", {"WAYLAND_DISPLAY": "wayland-0"}, True, "browser"),
        ("Linux", {"DISPLAY": ":0", "CI": "true"}, True, "manual"),
        ("Windows", {"SESSIONNAME": "Console"}, True, "browser"),
        ("Windows", {"SESSIONNAME": "Services"}, True, "manual"),
        ("Darwin", {"TERM_PROGRAM": "iTerm.app"}, False, "manual"),
    ],
)
def test_environment_only_recommends(monkeypatch, system, environment, tty, expected):
    monkeypatch.setattr("aero.core.device_login.platform.system", lambda: system)
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty)
    monkeypatch.setattr("sys.stdout.isatty", lambda: tty)
    opened = Mock(side_effect=AssertionError("detection must not open anything"))
    monkeypatch.setattr("webbrowser.open", opened)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    assert login_recommendation()[0] == expected
    opened.assert_not_called()


async def test_browser_failure_keeps_same_request_and_hides_secrets(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=START)

    monkeypatch.setattr("webbrowser.open", Mock(return_value=False))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        flow = DeviceLoginController(OfficialAccountSession(client=http))
        assert flow.public_state()["state"] == "idle" and not requests
        public = await flow.start("browser")
        assert "验证码授权" in public["message"]
        assert public["mode"] == "manual"
        assert "device_code" not in json.dumps(public) and "access_token" not in public
        assert START["device_code"] not in json.dumps(public)
        assert public["verification_uri_complete"].endswith("user_code=ABCD-EFGH&mode=browser")
        assert json.loads(requests[0].content)["client_version"] == get_version()
        assert len(requests) == 1


async def test_browser_and_code_modes_reuse_one_request(monkeypatch):
    opened = Mock(return_value=True)
    monkeypatch.setattr("webbrowser.open", opened)
    responses = []

    def handle(request):
        responses.append(request)
        return httpx.Response(200, json=START)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        flow = DeviceLoginController(OfficialAccountSession(client=http))
        browser = await flow.start("browser")
        assert browser["mode"] == "browser"
        assert START["user_code"] not in authorization_status(browser)
        assert START["verification_uri"] not in authorization_status(browser)
        opened.assert_called_once_with(browser["verification_uri_complete"])
        flow.use_code_authorization()
        manual = flow.public_state()
        assert manual["flow_id"] == browser["flow_id"]
        assert START["user_code"] in authorization_status(manual)
        assert START["verification_uri"] in authorization_status(manual)
        await flow.open_browser()
        assert flow.public_state()["mode"] == "browser"
        assert len(responses) == 1


async def test_manual_choice_does_not_open_browser(monkeypatch):
    opened = Mock(side_effect=AssertionError("manual mode must not open a browser"))
    monkeypatch.setattr("webbrowser.open", opened)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=START))
    ) as http:
        flow = DeviceLoginController(OfficialAccountSession(client=http))
        manual = await flow.start("manual")
        assert START["user_code"] in authorization_status(manual)
        opened.assert_not_called()


@pytest.mark.parametrize("host", ["standalone", "chat"])
async def test_login_action_keys_move_visible_focus_and_enter_runs_selected_action(
    monkeypatch, host
):
    never = asyncio.Event()

    async def waiting(*args):
        await never.wait()

    monkeypatch.setattr(
        DeviceLoginController, "_post", AsyncMock(return_value=httpx.Response(200, json=START))
    )
    monkeypatch.setattr(DeviceLoginController, "wait", waiting)
    monkeypatch.setattr(DeviceLoginController, "cancel", AsyncMock())
    monkeypatch.setattr("webbrowser.open", Mock(return_value=True))
    if host == "chat":
        from aero.cli.main import AeroApp
        from aero.core.config import AeroConfig

        app = AeroApp(AeroConfig.create_default(), persist_config=False)
        monkeypatch.setattr(app, "on_mount", lambda: None)
        monkeypatch.setattr(app, "action_cycle_mode", Mock(side_effect=AssertionError("chat key")))
    else:
        app = OfficialLoginApp("browser")
    async with app.run_test(size=(100, 32)) as pilot:
        if host == "chat":
            app.push_screen(OfficialLoginScreen("browser"))
        await pilot.pause()
        screen = app.screen
        assert app.focused.id == "login-cancel"
        for key, expected in [
            ("tab", "login-open"), ("right", "login-manual"),
            ("left", "login-open"), ("shift+tab", "login-cancel"),
            ("left", "login-manual"),
        ]:
            await pilot.press(key)
            assert app.focused.id == expected
            assert app.focused.has_pseudo_class("focus")
            assert app.focused.styles.background == Color.parse("#1676b8")
        await pilot.press("enter")
        assert screen.controller.public_state()["mode"] == "manual"
        assert START["user_code"] in str(screen.query_one("#official-login-status").render())
        await pilot.press("left", "enter")
        assert screen.controller.public_state()["mode"] == "browser"
        assert START["user_code"] not in str(screen.query_one("#official-login-status").render())
        screen._update({"state": "expired", "mode": "browser", "message": "授权请求已过期"})
        await pilot.pause()
        assert app.focused.id == "login-retry"
        await pilot.press("tab")
        assert app.focused.id == "login-cancel"
        await pilot.press("shift+tab")
        assert app.focused.id == "login-retry"
        await pilot.press("escape")


async def test_cancelled_late_token_does_not_replace_existing_login():
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = []

    async def handle(request):
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json=START)
        if request.url.path.endswith("/cancel"):
            cancelled.append(request)
            return httpx.Response(200, json={"status": "cancelled"})
        entered.set()
        await release.wait()
        return httpx.Response(200, json=TOKENS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        flow = DeviceLoginController(OfficialAccountSession(client=http))
        await flow.start("manual")
        job = asyncio.create_task(flow.wait())
        await entered.wait()
        await flow.cancel()
        release.set()
        assert await job is None
        assert cancelled and load_official_session().user_id == "existing-user"
        assert flow.public_state()["state"] == "cancelled"


@pytest.mark.parametrize(
    "payload,status,state",
    [
        ({"error": "access_denied"}, 400, "denied"),
        ({"error": "expired_token"}, 400, "expired"),
        ({"error": "invalid_grant"}, 400, "failed"),
        ({"bad": "reply"}, 503, "failed"),
    ],
)
async def test_failure_preserves_existing_account(payload, status, state):
    attempts = []

    def handle(request):
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json=START)
        attempts.append(request.url.path)
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        flow = DeviceLoginController(OfficialAccountSession(client=http))
        await flow.start("manual")
        flow._interval = 0.001
        assert await flow.wait() is None
        assert flow.public_state()["state"] == state
        assert load_official_session().access_token == "old-access"
        assert attempts.count("/v1/auth/device-login/token") <= 3


async def test_cancel_after_terminal_screen_and_closed_connection_is_safe():
    async def handle(request):
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json=START)
        return httpx.Response(400, json={"error": "access_denied"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        flow = DeviceLoginController(OfficialAccountSession(client=http))
        await flow.start("manual")
        assert await flow.wait() is None
    await flow.cancel()
    assert flow.public_state()["state"] == "cancelled"
    assert load_official_session().user_id == "existing-user"


@pytest.mark.parametrize(
    "payload", [[], {**START, "verification_uri": 123}, {**START, "interval": "Infinity"}]
)
async def test_malformed_authorization_response_is_bounded(payload):
    from aero.core.official_account import OfficialAccountError

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        flow = DeviceLoginController(OfficialAccountSession(client=http))
        with pytest.raises(OfficialAccountError):
            await flow.start("manual")
        assert load_official_session().access_token == "old-access"


async def test_polling_does_not_hold_account_lock_or_block_refresh():
    waiting, release = asyncio.Event(), asyncio.Event()

    async def handle(request):
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json=START)
        if request.url.path.endswith("/refresh"):
            return httpx.Response(200, json={**TOKENS, "user_id": "existing-user"})
        if request.url.path.endswith("/token"):
            waiting.set()
            await release.wait()
        return httpx.Response(400, json={"error": "access_denied"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        account = OfficialAccountSession(client=http)
        flow = DeviceLoginController(account)
        await flow.start("manual")
        task = asyncio.create_task(flow.wait())
        await waiting.wait()
        refreshed = await asyncio.wait_for(account.refresh(force=True), 1)
        assert refreshed.user_id == "existing-user"
        release.set()
        assert await task is None
        assert load_official_session().access_token == "new-access"


async def test_expiry_during_inflight_exchange_does_not_save_late_tokens():
    async def handle(request):
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json=START)
        if request.url.path.endswith("/token"):
            flow._deadline = 0
            return httpx.Response(200, json=TOKENS)
        return httpx.Response(200, json={"status": "cancelled"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        flow = DeviceLoginController(OfficialAccountSession(client=http))
        await flow.start("manual")
        assert await flow.wait() is None
        assert flow.public_state()["state"] == "expired"
        assert load_official_session().user_id == "existing-user"


async def test_screen_focus_navigation_and_no_request_before_choice(monkeypatch):
    start = AsyncMock(side_effect=AssertionError("choice required"))
    monkeypatch.setattr(DeviceLoginController, "start", start)
    app = OfficialLoginApp()
    async with app.run_test(size=(80, 22)) as pilot:
        await pilot.pause()
        assert isinstance(app.focused, OptionList)
        assert app.screen.query_one("#official-login-dialog").region.right <= 80
        await pilot.press("down", "tab", "shift+tab")
        assert isinstance(app.focused, OptionList)
        await pilot.press("escape")
    start.assert_not_awaited()


@pytest.mark.parametrize("size", [(80, 22), (160, 48)])
async def test_login_dialog_is_centered_above_chat_and_after_resize(size):
    class Host(App):
        def compose(self) -> ComposeResult:
            with Vertical():
                yield Static("chat")
                yield Static("input", id="input-box")

        def on_mount(self):
            self.query_one("#input-box").styles.width = 82
            self.push_screen(OfficialLoginScreen())

    app = Host()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        region = app.screen.query_one("#official-login-dialog").region
        assert abs(region.x * 2 + region.width - size[0]) <= 1
        assert abs(region.y * 2 + region.height - size[1]) <= 1
        assert region.bottom < size[1]
        await pilot.resize_terminal(120, 36)
        await pilot.pause()
        region = app.screen.query_one("#official-login-dialog").region
        assert abs(region.x * 2 + region.width - 120) <= 1
        assert abs(region.y * 2 + region.height - 36) <= 1
        await pilot.press("escape")


async def test_local_web_authorization_never_opens_daemon_browser_or_leaks_tokens(monkeypatch):
    from fastapi import FastAPI

    release = asyncio.Event()

    async def handle(request):
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json=START)
        if request.url.path.endswith("/token"):
            await release.wait()
            return httpx.Response(200, json=TOKENS)
        return httpx.Response(200, json={"status": "cancelled"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        account = OfficialAccountSession(client=upstream)
        monkeypatch.setattr("aero.server.cloud.OfficialAccountSession", lambda: account)
        opened = Mock(side_effect=AssertionError("must use the user's browser"))
        monkeypatch.setattr("webbrowser.open", opened)
        web = SimpleNamespace()
        service = CloudServices(web)
        web.cloud = service

        async def require_auth():
            return web

        app = FastAPI()
        app.include_router(cloud_router(require_auth))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            endpoint = "/api/v1/cloud/account/device-login"
            assert (
                await client.post(endpoint + "/start", json={"mode": "manual", "password": "x"})
            ).status_code == 422
            started = (await client.post(endpoint + "/start", json={"mode": "browser"})).json()
            public = (await client.get(endpoint + "/" + started["flow_id"])).json()
            assert "private" not in json.dumps(public) and "token" not in json.dumps(public)
            assert (await client.get(endpoint + "/wrong-flow")).status_code == 404
            assert (await client.post("/api/v1/cloud/account/login", json={})).status_code == 410
            await client.post(endpoint + "/" + started["flow_id"] + "/cancel")
            assert load_official_session().user_id == "existing-user"
            opened.assert_not_called()
        await service.cancel_login()


@pytest.mark.parametrize("uid", ["existing-user", "other-user"])
async def test_web_account_commit_pauses_cloud_and_only_resumes_same_user(
    monkeypatch, tmp_path, uid
):
    from aero.core.config import AeroConfig

    def handle(request):
        return httpx.Response(
            200, json=(START if request.url.path.endswith("/start") else {**TOKENS, "user_id": uid})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        monkeypatch.setattr(
            "aero.server.cloud.OfficialAccountSession", lambda: OfficialAccountSession(client=http)
        )
        web = SimpleNamespace(
            config=AeroConfig(), project_dir=tmp_path, sync_active_sessions=Mock()
        )
        service = CloudServices(web)
        service._sync = SimpleNamespace(status=lambda: {"enabled": True}, pause=Mock())
        service.pause_cloud = AsyncMock()
        service.start_sync = Mock()
        await service.start_login("manual")
        await service._login_task
        assert load_official_session().user_id == uid
        service.pause_cloud.assert_awaited_once()
        assert service.start_sync.call_count == int(uid == "existing-user")
        public = service._login_controller.public_state()
        assert public["state"] == "approved"
        assert "device_code" not in public and "access_token" not in public
        assert web.config.llm.provider == "official"
        web.sync_active_sessions.assert_called_once()


async def test_setup_reuses_login_selector_without_requesting_password(monkeypatch):
    from aero.cli.setup_wizard import FirstRunSetupScreen

    start = AsyncMock(side_effect=AssertionError("select a method first"))
    monkeypatch.setattr(DeviceLoginController, "start", start)
    monkeypatch.setattr("aero.cli.setup_wizard.BYOK_UI_ENABLED", False)

    class Host(App):
        def on_mount(self):
            self.push_screen(FirstRunSetupScreen())

    app = Host()
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, OfficialLoginScreen)
        assert isinstance(app.focused, OptionList)
        await pilot.press("escape")
    start.assert_not_awaited()
