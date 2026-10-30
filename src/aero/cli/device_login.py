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
    BINDINGS = [Binding("escape", "cancel", "取消登录", priority=True)]

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
                    "在本机浏览器授权" + ("（推荐）" if recommendation == "browser" else ""),
                    id="browser",
                ),
                Option(
                    "使用网址和授权码" + ("（推荐）" if recommendation == "manual" else ""),
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
                yield QuestionAction("手动操作", id="login-manual")
                yield QuestionAction("重新获取", id="login-retry")
                yield QuestionAction("取消", id="login-cancel")

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
        self.query_one("#login-retry").display = False
        self.query_one("#official-login-status", Static).update("正在获取授权码…")
        self._login_worker = self.run_worker(
            self._authorize(mode), exclusive=True, group="device-login"
        )

    def _update(self, state: dict) -> None:
        if not self.is_mounted:
            return
        terminal = state["state"] in TERMINAL_STATES
        url = state.get("verification_uri", "")
        self.query_one("#official-login-status", Static).update(
            (
                f"网址：{url}\n授权码：{state.get('user_code', '')}\n"
                f"有效期剩余：{state.get('expires_in', 0)} 秒\n"
                if url
                else ""
            )
            + state.get("message", "正在等待官网授权…")
        )
        for name in ("open", "manual"):
            self.query_one("#login-" + name).display = bool(url) and not terminal
        self.query_one("#login-retry").display = terminal and state["state"] != "approved"

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
                self.query_one("#official-login-status", Static).update(str(exc))
                self.query_one("#login-retry").display = True
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
            self.controller._state.update(
                mode="manual", message="请手动访问网址并输入同一个授权码。"
            )
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
        print(
            f"网址：{state['verification_uri']}\n授权码：{state['user_code']}\n"
            f"有效期：{state['expires_in']} 秒；正在等待官网授权…",
            flush=True,
        )
        result = await controller.wait()
        print(controller.public_state().get("message", ""))
        return result is not None
    except (OfficialAccountError, KeyboardInterrupt):
        await controller.cancel()
        return False
    finally:
        await session.close()
