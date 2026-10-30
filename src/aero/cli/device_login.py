"""Centered account authorization; never collects an account password."""

from __future__ import annotations

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from aero.cli.question_screen import QuestionAction
from aero.core.device_login import TERMINAL_STATES, DeviceLoginController, login_recommendation
from aero.core.official_account import (
    OfficialAccountError,
    OfficialAccountSession,
    OfficialSessionData,
)


def authorization_status(state: dict) -> str:
    """Browser authorization has no visible URL or code-entry step."""
    details = ""
    if state.get("verification_uri"):
        if state.get("mode") == "manual":
            details = f"网址：{state['verification_uri']}\n验证码：{state.get('user_code', '')}\n"
        else:
            details = "浏览器授权：请在网页中批准登录，无需输入验证码。\n"
        details += f"有效期剩余：{state.get('expires_in', 0)} 秒\n"
    return details + state.get("message", "正在等待官网授权…")


class OfficialLoginScreen(ModalScreen[OfficialSessionData | None]):
    CSS = """
    OfficialLoginScreen { align: center middle; background: $background 90%; }
    #official-login-dialog {
        width: 90%; max-width: 100; height: auto; max-height: 90%; overflow-y: auto;
        border: solid #5dade2; background: $surface; padding: 1 2; margin: 0;
    }
    #official-login-options { height: auto; max-height: 6; border: none; }
    #official-login-status { height: auto; margin: 1 0; }
    #official-login-actions { height: 1; min-height: 1; }
    #official-login-actions QuestionAction {
        width: 1fr; max-width: 14; height: 1; margin-right: 1; padding: 0;
        content-align: center middle; background: $boost; color: $text;
    }
    #official-login-actions QuestionAction:focus { background: #1676b8; color: #ffffff; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "取消登录", priority=True),
        Binding("tab,right", "login_focus(1)", "下一项", priority=True),
        Binding("shift+tab,left", "login_focus(-1)", "上一项", priority=True),
    ]

    def __init__(self, mode: str | None = None):
        super().__init__()
        self.mode = mode
        self.controller: DeviceLoginController | None = None
        self._login_worker = None

    def compose(self) -> ComposeResult:
        recommendation, reason = login_recommendation()
        with Vertical(id="official-login-dialog"):
            yield Static("登录 Aerolytica 官方账户", markup=False)
            yield Static(reason + " 登录方式由你决定。", markup=False)
            yield OptionList(
                Option(
                    "浏览器授权" + ("（推荐）" if recommendation == "browser" else ""),
                    id="browser",
                ),
                Option(
                    "验证码授权" + ("（推荐）" if recommendation == "manual" else ""),
                    id="manual",
                ),
                Option("取消", id="cancel"),
                id="official-login-options",
            )
            yield Static(
                "↑↓ 选择 · Enter 确认 · Tab 切换 · Esc 取消",
                id="official-login-status",
                markup=False,
            )
            with Horizontal(id="official-login-actions"):
                yield QuestionAction("打开网页", id="login-open")
                yield QuestionAction("验证码授权", id="login-manual")
                yield QuestionAction("重新授权", id="login-retry")
                yield QuestionAction("取消", id="login-cancel")

    def action_login_focus(self, direction: int) -> None:
        controls = []
        options = self.query_one("#official-login-options")
        if options.display:
            controls.append(options)
        controls.extend(
            button for button in self.query_one("#official-login-actions").query(QuestionAction)
            if button.display and not button.disabled
        )
        if controls:
            index = controls.index(self.focused) if self.focused in controls else (
                -1 if direction > 0 else 0
            )
            controls[(index + direction) % len(controls)].focus()

    def on_mount(self) -> None:
        for name in ("open", "manual", "retry"):
            self.query_one("#login-" + name).display = False
        options = self.query_one("#official-login-options", OptionList)
        options.highlighted = 0 if login_recommendation()[0] == "browser" else 1
        options.focus()
        if self.mode:
            self._begin(self.mode)

    @on(OptionList.OptionSelected, "#official-login-options")
    async def selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        mode = str(event.option.id)
        if mode == "cancel":
            await self.action_cancel()
        else:
            self._begin(mode)

    def _begin(self, mode: str) -> None:
        if self._login_worker and not self._login_worker.is_finished:
            return
        self.mode = mode
        self.query_one("#official-login-options").display = False
        for name in ("open", "manual", "retry"):
            self.query_one("#login-" + name).display = False
        self.query_one("#login-cancel").focus()
        self.query_one("#official-login-status", Static).update("正在准备登录授权…")
        self._login_worker = self.run_worker(
            self._authorize(mode), exclusive=True, group="device-login"
        )

    def _update(self, state: dict) -> None:
        if not self.is_mounted:
            return
        terminal = state["state"] in TERMINAL_STATES
        self.mode = state.get("mode", self.mode)
        url = state.get("verification_uri", "")
        self.query_one("#official-login-status", Static).update(authorization_status(state))
        for name in ("open", "manual"):
            self.query_one("#login-" + name).display = bool(url) and not terminal
        self.query_one("#login-retry").display = terminal and state["state"] != "approved"
        if terminal and state["state"] != "approved":
            self.query_one("#login-retry").focus()

    async def _authorize(self, mode: str) -> None:
        session = OfficialAccountSession()
        self.controller = DeviceLoginController(session)
        try:
            self._update(await self.controller.start(mode))
            self.query_one("#login-cancel").focus()
            result = await self.controller.wait(self._update)
            if result and self.is_mounted:
                self.dismiss(result)
            else:
                self._update(self.controller.public_state())
        except OfficialAccountError as exc:
            if self.is_mounted:
                self._update({**self.controller.public_state(), "message": str(exc)})
        finally:
            await session.close()

    @on(QuestionAction.Pressed)
    async def pressed(self, event: QuestionAction.Pressed) -> None:
        event.stop()
        action = event.control.id
        if action == "login-cancel":
            await self.action_cancel()
        elif action == "login-retry":
            self._begin(self.mode or "manual")
        elif self.controller and action == "login-open":
            await self.controller.open_browser()
            self._update(self.controller.public_state())
        elif self.controller and action == "login-manual":
            self.controller.use_code_authorization()
            self._update(self.controller.public_state())

    async def action_cancel(self) -> None:
        if self.controller:
            await self.controller.cancel()
        if self._login_worker:
            self._login_worker.cancel()
        self.dismiss(None)

    async def on_unmount(self) -> None:
        if self.controller and self.controller.public_state()["state"] not in TERMINAL_STATES:
            await self.controller.cancel()
        if self._login_worker:
            self._login_worker.cancel()


class OfficialLoginApp(App[bool]):
    def __init__(self, mode: str | None = None):
        super().__init__()
        self.mode = mode

    def on_mount(self) -> None:
        self.push_screen(OfficialLoginScreen(self.mode), lambda result: self.exit(bool(result)))


async def console_login(mode: str) -> bool:
    session = OfficialAccountSession()
    controller = DeviceLoginController(session)
    try:
        state = await controller.start(mode)
        print(authorization_status(state), flush=True)
        result = await controller.wait()
        print(controller.public_state().get("message", ""))
        return result is not None
    except (OfficialAccountError, KeyboardInterrupt):
        await controller.cancel()
        return False
    finally:
        await session.close()
