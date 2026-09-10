"""Aero CLI — Textual TUI and command dispatcher."""

import asyncio
import copy
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

# Textual's enhanced Kitty keyboard protocol can interfere with Chinese IME
# commits in some terminal / macOS combinations, turning committed text into
# modifier events or spaces. Keep it disabled by default for TUI chat input.
if os.environ.get("AERO_ENABLE_KITTY_KEY") != "1":
    os.environ["TEXTUAL_DISABLE_KITTY_KEY"] = "1"

# The Web server never renders terminal images. Avoid probing terminal escape
# sequences when ``aero serve`` is invoked from a non-interactive process.
if len(sys.argv) > 1 and sys.argv[1] == "serve":
    os.environ["AERO_TEXTUAL_IMAGE_PROTOCOL"] = "0"

from rich.markup import escape
from rich.table import Table
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Offset
from textual.screen import ModalScreen
from textual.scrollbar import ScrollBarRender
from textual.widgets import (
    Button,
    Input,
    ListItem,
    ListView,
    Markdown,
    OptionList,
    Static,
    TextArea,
)
from textual.widgets._markdown import MarkdownBullet, MarkdownBulletList, MarkdownListItem
from textual.widgets._option_list import Option
from textual.worker import Worker, WorkerCancelled

TerminalImage = None
if os.environ.get("AERO_TEXTUAL_IMAGE_PROTOCOL") != "0":
    try:
        from textual_image.widget import Image as _TerminalImage

        renderable_module = getattr(
            getattr(_TerminalImage, "_Renderable", None),
            "__module__",
            "",
        )
        if not renderable_module.endswith(".unicode"):
            TerminalImage = _TerminalImage
    except Exception:
        TerminalImage = None

from aero.cli.image_widget import (
    extract_image_paths,
    image_metadata,
    resolve_image_path,
    strip_image_markdown,
    terminal_half_block_preview,
    terminal_image_preview,
)
from aero.agent.subagent import (
    SubAgentManager,
    SubAgentTask,
    use_subagent_canceller,
    use_subagent_launcher,
    use_subagent_status_provider,
)
from aero.agent.checkpoint_context import use_checkpoint_creator
from aero.core.config import (
    AeroConfig,
    clear_llm_api_key,
    resolved_vision_config,
    save_cds_credentials,
    save_llm_profile,
    save_vision_api_key,
    save_vision_profile,
    save_web_search_api_key,
    save_web_search_state,
    vision_is_configured,
)
from aero.core.debug_log import configure_debug_logging, debug_log
from aero.core.llm_providers import (
    OFFICIAL_ACCOUNT_UI_ENABLED,
    get_provider_preset,
    model_alias_for_provider,
    normalize_provider_id,
    provider_options,
)
from aero.core.logging import configure as configure_logging
from aero.data.plans import set_session_id
from aero.data.pricing import TokenTracker, context_window_for, format_cost, format_token_count
from aero.core.types import Message
from aero.i18n import is_supported_language, language_label, language_options, t
from aero.paper_versions import PaperVersionManager
from aero.paper_export import PaperExportError, export_paper
from aero.toolbox.paths import use_workspace
from aero.toolbox.secret_input import (
    SecretInputRequest,
    get_credential_spec,
    use_secret_input_provider,
)

DEEPSEEK_MODEL_ALIASES = {
    "flash": "deepseek-v4-flash",
    "v4-flash": "deepseek-v4-flash",
    "pro": "deepseek-v4-pro",
    "v4-pro": "deepseek-v4-pro",
}
REASONING_EFFORTS = {"low", "medium", "high", "max", "xhigh"}
_REUSE_PRIMARY_VISION_OPTION = "__reuse_primary_vision__"
DEEPSEEK_API_KEYS_URL = "https://platform.deepseek.com/api_keys"
DEEPSEEK_DOCS_URL = "https://api-docs.deepseek.com/"

STARTUP_LOGO = "── A E R O L Y T I C A ──"

_CONFIRM_ACTION_LABELS = {
    "delete_file": "删除文件",
    "run_shell": "执行 Shell 命令",
    "ensure_runtime_tools": "安装命令行工具",
    "record_memo": "加入研究备忘录",
    "update_memo": "更新研究备忘录",
    "delete_memo": "删除研究备忘录",
    "clear_memos": "清理研究备忘录",
    "restore_paper_version": "恢复论文版本",
}


def _truncate_middle(text: str, max_width: int) -> str:
    if len(text) <= max_width:
        return text
    if max_width <= 8:
        return text[:max_width]
    left = (max_width - 1) // 2
    right = max_width - left - 1
    return f"{text[:left]}…{text[-right:]}"


def _format_tool_list(value: Any) -> str:
    if not isinstance(value, list):
        return "未知"
    tools = [str(item).strip() for item in value if str(item).strip()]
    return "、".join(tools) if tools else "未知"


def _strip_markup(text: str) -> str:
    return re.sub(r"\[/?[^\]]+\]", "", text)


class ExecutionApprovalScreen(ModalScreen[str]):
    """Approval dialog for switching from plan mode to execute mode."""

    CSS = """
    ExecutionApprovalScreen {
        align: left bottom;
        background: transparent;
    }

    #exec-dialog {
        width: 1fr;
        max-width: 140;
        height: auto;
        max-height: 22;
        border: solid #5dade2;
        background: $surface;
        padding: 1 0 0 2;
        margin: 0 2 1 10;
    }

    #exec-title {
        text-style: bold;
        color: $text;
        width: 100%;
        content-align: left middle;
    }

    #exec-message-box {
        width: 100%;
        height: auto;
        max-height: 12;
        margin: 1 2 1 0;
        overflow-y: auto;
    }

    #exec-message {
        width: 100%;
        color: $text;
    }

    #exec-buttons {
        width: 100%;
        height: 3;
        background: $boost;
        align: left middle;
        padding: 0 2 0 2;
    }

    .exec-option {
        width: 16;
        margin: 0 1 0 0;
        background: $boost;
        color: $text-muted;
        content-align: center middle;
    }

    .exec-option.exec-selected {
        background: #5dade2;
        color: $text;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("escape", "defer", "暂不", show=False, priority=True),
        Binding("n", "defer", "暂不", show=False, priority=True),
        Binding("y", "approve", "开始", show=False, priority=True),
        Binding("a", "approve", "开始", show=False, priority=True),
        Binding("tab", "focus_next", show=False, priority=True),
        Binding("left", "focus_previous", show=False, priority=True),
        Binding("right", "focus_next", show=False, priority=True),
        Binding("enter", "confirm_selected", "确认", show=False, priority=True),
        Binding("space", "confirm_selected", "确认", show=False, priority=True),
    ]

    _BUTTON_IDS = ("#btn-start", "#btn-defer")

    def __init__(self, lang: str = "zh"):
        super().__init__()
        self._lang = lang
        self._selected_button = 0

    def on_mount(self) -> None:
        self._sync_selected_button()

    def compose(self) -> ComposeResult:
        with Vertical(id="exec-dialog"):
            yield Static("△ 执行确认", id="exec-title")
            with VerticalScroll(id="exec-message-box"):
                yield Static("方案已完成，是否切换到执行模式开始构建？", id="exec-message")
            with Horizontal(id="exec-buttons"):
                yield Static("开始执行", id="btn-start", classes="exec-option")
                yield Static("暂不执行", id="btn-defer", classes="exec-option")

    def on_key(self, event: events.Key) -> None:
        if event.key == "left":
            event.stop()
            event.prevent_default()
            self.action_focus_previous()
            return
        if event.key in {"right", "tab"}:
            event.stop()
            event.prevent_default()
            self.action_focus_next()
            return
        if event.key in {"enter", "space"}:
            event.stop()
            event.prevent_default()
            self._confirm_selected_button()
            return

    @on(events.Click, "#btn-start")
    def on_approve_pressed(self) -> None:
        self._selected_button = 0
        self.action_approve()

    @on(events.Click, "#btn-defer")
    def on_defer_pressed(self) -> None:
        self._selected_button = 1
        self.action_defer()

    def action_focus_previous(self) -> None:
        self._selected_button = (self._selected_button - 1) % len(self._BUTTON_IDS)
        self._sync_selected_button()

    def action_focus_next(self) -> None:
        self._selected_button = (self._selected_button + 1) % len(self._BUTTON_IDS)
        self._sync_selected_button()

    def action_approve(self) -> None:
        self.dismiss("approve")

    def action_defer(self) -> None:
        self.dismiss("defer")

    def _confirm_selected_button(self) -> None:
        action = "approve" if self._selected_button == 0 else "defer"
        self.dismiss(action)

    def _sync_selected_button(self) -> None:
        for i, bid in enumerate(self._BUTTON_IDS):
            try:
                btn = self.query_one(bid, Static)
            except Exception:
                continue
            if i == self._selected_button:
                btn.add_class("exec-selected")
            else:
                btn.remove_class("exec-selected")


class ConfirmScreen(ModalScreen[str]):
    """Modal confirmation dialog for dangerous tool operations."""

    CSS = """
    ConfirmScreen {
        align: left bottom;
    }

    #confirm-dialog {
        width: 1fr;
        max-width: 140;
        height: auto;
        max-height: 22;
        border: solid $warning;
        background: $surface;
        padding: 1 0 0 2;
        margin: 0 2 1 10;
    }

    #confirm-title {
        text-style: bold;
        color: $text;
        width: 100%;
        content-align: left middle;
    }

    #confirm-message-box {
        width: 100%;
        height: auto;
        max-height: 12;
        margin: 1 2 1 0;
        overflow-y: auto;
    }

    #confirm-message {
        width: 100%;
        color: $text;
    }

    #confirm-buttons {
        width: 100%;
        height: 3;
        background: $boost;
        align: left middle;
        padding: 0 2 0 2;
    }

    .confirm-option {
        width: 16;
        margin: 0 1 0 0;
        background: $boost;
        color: $text-muted;
        content-align: center middle;
    }

    .confirm-option.confirm-selected {
        background: $warning;
        color: $text;
        text-style: bold;
    }

    #confirm-shortcuts {
        width: 1fr;
        content-align: right middle;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "deny", "拒绝", show=False, priority=True),
        Binding("n", "deny", "拒绝", show=False, priority=True),
        Binding("d", "deny", "拒绝", show=False, priority=True),
        Binding("y", "allow", "允许一次", show=False, priority=True),
        Binding("a", "allow", "允许一次", show=False, priority=True),
        Binding("shift+a", "always", "本次会话允许", show=False, priority=True),
        Binding("tab", "focus_next", "右移", show=False, priority=True),
        Binding("left", "focus_previous", "左移", show=False, priority=True),
        Binding("right", "focus_next", "右移", show=False, priority=True),
        Binding("up", "scroll_message_up", "上滚", show=False, priority=True),
        Binding("down", "scroll_message_down", "下滚", show=False, priority=True),
        Binding("pageup", "scroll_message_page_up", "向上翻页", show=False, priority=True),
        Binding("pagedown", "scroll_message_page_down", "向下翻页", show=False, priority=True),
        Binding("home", "scroll_message_home", "滚到顶部", show=False, priority=True),
        Binding("end", "scroll_message_end", "滚到底部", show=False, priority=True),
        Binding("enter", "confirm_selected", "确认", show=False, priority=True),
        Binding("space", "confirm_selected", "确认", show=False, priority=True),
    ]

    def __init__(
        self,
        message: str,
        lang: str = "zh",
        *,
        title: str | None = None,
        allow_label: str | None = None,
        deny_label: str | None = None,
        show_always: bool = True,
    ):
        super().__init__()
        self._message = message
        self._lang = lang
        self._title = title or t("confirm.permission_required", lang)
        self._allow_label = allow_label or t("confirm.allow_once", lang)
        self._deny_label = deny_label or t("confirm.reject", lang)
        self._show_always = show_always
        self._button_ids = (
            ("#btn-allow", "#btn-always", "#btn-deny")
            if show_always
            else ("#btn-allow", "#btn-deny")
        )
        self._selected_button = 0

    def on_mount(self) -> None:
        self._sync_selected_button()

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(self._title, id="confirm-title")
            with VerticalScroll(id="confirm-message-box"):
                yield Static(self._message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Static(self._allow_label, id="btn-allow", classes="confirm-option")
                if self._show_always:
                    yield Static(
                        t("confirm.allow_always", self._lang),
                        id="btn-always",
                        classes="confirm-option",
                    )
                yield Static(self._deny_label, id="btn-deny", classes="confirm-option")
                yield Static(t("confirm.shortcuts", self._lang), id="confirm-shortcuts")

    def on_key(self, event: events.Key) -> None:
        scroll_actions = {
            "up": self.action_scroll_message_up,
            "down": self.action_scroll_message_down,
            "pageup": self.action_scroll_message_page_up,
            "pagedown": self.action_scroll_message_page_down,
            "home": self.action_scroll_message_home,
            "end": self.action_scroll_message_end,
        }
        scroll_action = scroll_actions.get(event.key)
        if scroll_action is not None:
            event.stop()
            event.prevent_default()
            scroll_action()
            return
        if event.key == "left":
            event.stop()
            event.prevent_default()
            self.action_focus_previous()
            return
        if event.key in {"right", "tab"}:
            event.stop()
            event.prevent_default()
            self.action_focus_next()
            return
        if event.key in {"enter", "space"}:
            event.stop()
            event.prevent_default()
            self._confirm_selected_button()
            return
        if event.character == "A" and self._show_always:
            event.stop()
            self.action_always()

    @on(events.Click, "#btn-allow")
    def on_allow_pressed(self) -> None:
        self._selected_button = self._button_ids.index("#btn-allow")
        self.action_allow()

    @on(events.Click, "#btn-always")
    def on_always_pressed(self) -> None:
        if not self._show_always:
            return
        self._selected_button = self._button_ids.index("#btn-always")
        self.action_always()

    @on(events.Click, "#btn-deny")
    def on_deny_pressed(self) -> None:
        self._selected_button = self._button_ids.index("#btn-deny")
        self.action_deny()

    def action_focus_previous(self) -> None:
        self._move_button_focus(-1)

    def action_focus_next(self) -> None:
        self._move_button_focus(1)

    def action_confirm_selected(self) -> None:
        self._confirm_selected_button()

    def action_scroll_message_up(self) -> None:
        self._message_box().scroll_up(animate=False)

    def action_scroll_message_down(self) -> None:
        self._message_box().scroll_down(animate=False)

    def action_scroll_message_page_up(self) -> None:
        self._message_box().scroll_page_up(animate=False)

    def action_scroll_message_page_down(self) -> None:
        self._message_box().scroll_page_down(animate=False)

    def action_scroll_message_home(self) -> None:
        self._message_box().scroll_home(animate=False)

    def action_scroll_message_end(self) -> None:
        self._message_box().scroll_end(animate=False)

    def _message_box(self) -> VerticalScroll:
        return self.query_one("#confirm-message-box", VerticalScroll)

    def _move_button_focus(self, delta: int) -> None:
        self._selected_button = (self._selected_button + delta) % len(self._button_ids)
        self._sync_selected_button()

    def _sync_selected_button(self) -> None:
        for index, selector in enumerate(self._button_ids):
            button = self.query_one(selector, Static)
            selected = index == self._selected_button
            button.set_class(selected, "confirm-selected")
            button.refresh(repaint=True)

    def _confirm_selected_button(self) -> None:
        selected = self._button_ids[self._selected_button]
        if selected == "#btn-allow":
            self.action_allow()
        elif selected == "#btn-always":
            self.action_always()
        else:
            self.action_deny()

    def action_allow(self) -> None:
        self.dismiss("allow")

    def action_always(self) -> None:
        if not self._show_always:
            return
        self.dismiss("always")

    def action_deny(self) -> None:
        self.dismiss("deny")


class SecretInputAction(Static):
    """Focusable text action used by the keyboard-first secret dialog."""

    can_focus = True


class SecretTextArea(TextArea):
    """Multiline secret editor where Enter submits and Shift+Enter adds a line."""

    BINDINGS = [
        Binding("enter", "submit_secret", show=False, priority=True),
        Binding("shift+enter", "newline", show=False, priority=True),
        *TextArea.BINDINGS,
    ]

    def action_submit_secret(self) -> None:
        screen = self.screen
        if isinstance(screen, SecretInputScreen):
            screen.action_submit()


class SecretInputScreen(ModalScreen[str | None]):
    """Local-only entry surface for a secret requested by a tool."""

    CSS = """
    SecretInputScreen { align: center middle; }
    #secret-input-dialog {
        width: 78%; max-width: 96; height: auto; max-height: 28;
        background: $surface; padding: 1 2;
    }
    #secret-input-title { text-style: bold; }
    #secret-input-note { color: $text-muted; margin: 1 0; }
    #secret-input-value { height: 8; border: solid $primary; }
    #secret-input-actions {
        width: 100%; height: 3; margin-top: 1;
        background: transparent; padding: 0; align: left middle;
    }
    .secret-input-action {
        width: auto; min-width: 8; padding: 0 1; margin: 0 1 0 0;
        background: transparent; color: $text-muted; content-align: center middle;
    }
    .secret-input-action.secret-input-selected {
        background: transparent; color: #5dade2; text-style: bold;
    }
    #secret-input-hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("tab", "focus_actions", show=False, priority=True),
        Binding("shift+tab", "focus_actions_previous", show=False, priority=True),
        Binding("escape", "cancel", "取消", show=False, priority=True),
    ]
    _ACTION_IDS = ("#secret-input-submit", "#secret-input-cancel")

    def __init__(self, request: SecretInputRequest):
        super().__init__()
        self._request = request
        self._action_focused = False
        self._selected_action = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="secret-input-dialog"):
            yield Static(self._request.title, id="secret-input-title")
            yield Static(self._request.instructions, id="secret-input-note")
            if self._request.multiline:
                yield SecretTextArea(id="secret-input-value", show_line_numbers=False)
            else:
                yield Input(password=True, id="secret-input-value")
            with Horizontal(id="secret-input-actions"):
                yield SecretInputAction(
                    "确认保存", id="secret-input-submit", classes="secret-input-action"
                )
                yield SecretInputAction(
                    "取消", id="secret-input-cancel", classes="secret-input-action"
                )
            yield Static(
                (
                    "Enter 确认保存 · Shift+Enter 换行 · Tab 切换到操作 · Esc 取消"
                    if self._request.multiline
                    else "Enter 确认保存 · Tab 切换到操作 · Esc 取消"
                ),
                id="secret-input-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#secret-input-value").focus()
        self._sync_selected_action()

    def on_key(self, event: events.Key) -> None:
        if self._action_focused:
            if event.key in {"left", "right", "tab", "shift+tab"}:
                event.stop()
                event.prevent_default()
                self._move_action_focus(-1 if event.key in {"left", "shift+tab"} else 1)
            elif event.key == "up":
                event.stop()
                event.prevent_default()
                self._leave_actions()
            elif event.key in {"enter", "space"}:
                event.stop()
                event.prevent_default()
                self._activate_selected_action()
            return

        if event.key == "tab":
            event.stop()
            event.prevent_default()
            self._focus_actions()
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            self.action_submit()

    def action_submit(self) -> None:
        widget = self.query_one("#secret-input-value")
        value = widget.text if isinstance(widget, TextArea) else widget.value
        self.dismiss(value.strip() or None)

    def action_focus_actions(self) -> None:
        self._focus_actions()

    def action_focus_actions_previous(self) -> None:
        self._focus_actions(last=True)

    @on(events.Click, "#secret-input-submit")
    def on_submit_pressed(self) -> None:
        self._selected_action = 0
        self.action_submit()

    @on(events.Click, "#secret-input-cancel")
    def on_cancel_pressed(self) -> None:
        self._selected_action = 1
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _focus_actions(self, *, last: bool = False) -> None:
        self._action_focused = True
        self._selected_action = len(self._ACTION_IDS) - 1 if last else 0
        self._sync_selected_action()
        self.query_one(self._ACTION_IDS[self._selected_action], SecretInputAction).focus()

    def _leave_actions(self) -> None:
        self._action_focused = False
        self._sync_selected_action()
        self.query_one("#secret-input-value").focus()

    def _move_action_focus(self, delta: int) -> None:
        self._selected_action = (self._selected_action + delta) % len(self._ACTION_IDS)
        self._sync_selected_action()
        self.query_one(self._ACTION_IDS[self._selected_action], SecretInputAction).focus()

    def _sync_selected_action(self) -> None:
        for index, selector in enumerate(self._ACTION_IDS):
            action = self.query_one(selector, Static)
            action.set_class(
                self._action_focused and index == self._selected_action,
                "secret-input-selected",
            )
            action.refresh(repaint=True)

    def _activate_selected_action(self) -> None:
        if self._selected_action == 0:
            self.action_submit()
        else:
            self.action_cancel()


class OfficialLoginScreen(ModalScreen[dict[str, str] | None]):
    """Collect official-account credentials without retaining the password."""

    CSS = """
    OfficialLoginScreen {
        align: center middle;
        background: $background 70%;
    }
    #official-login-dialog {
        width: 64;
        height: auto;
        padding: 1 2;
        border: solid #5dade2;
        background: $surface;
    }
    #official-login-dialog Input {
        margin: 1 0 0 0;
    }
    #official-login-actions {
        height: 3;
        margin-top: 1;
    }
    #official-login-actions Button {
        margin-right: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "取消", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="official-login-dialog"):
            yield Static("登录 Aerolytica 官方账户")
            yield Static(
                "登录后可使用官方模型服务并查询账户额度。",
                classes="setup-label",
            )
            yield Input(placeholder="邮箱", id="official-email")
            yield Input(placeholder="密码", password=True, id="official-password")
            with Horizontal(id="official-login-actions"):
                yield Button("登录", id="official-login-submit", variant="primary")
                yield Button("取消", id="official-login-cancel")

    @on(Button.Pressed, "#official-login-submit")
    def submit(self) -> None:
        email = self.query_one("#official-email", Input).value.strip()
        password = self.query_one("#official-password", Input).value
        if not email or not password:
            self.notify("请输入邮箱和密码。", severity="warning")
            return
        self.dismiss({"email": email, "password": password})

    @on(Button.Pressed, "#official-login-cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "official-email":
            self.query_one("#official-password", Input).focus()
        else:
            self.submit()

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Modal help panel for slash commands and shortcuts."""

    CSS = """
    HelpScreen {
        align: center middle;
    }

    #help-dialog {
        width: 82%;
        max-width: 110;
        height: auto;
        max-height: 34;
        background: $surface;
        padding: 1 2;
    }

    #help-header {
        width: 100%;
        height: 2;
    }

    #help-title {
        width: 1fr;
        text-style: bold;
    }

    #help-esc {
        width: 8;
        text-align: right;
        color: $text-muted;
    }

    #help-body {
        width: 100%;
        height: auto;
        max-height: 26;
        border: solid $primary;
        padding: 1 2;
        overflow-y: auto;
    }

    #help-content {
        width: 100%;
        color: $text;
    }

    #help-hint {
        margin-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "关闭", show=False, priority=True),
    ]

    def __init__(self, lang: str):
        super().__init__()
        self._lang = lang

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            with Horizontal(id="help-header"):
                yield Static(_strip_markup(t("help.title", self._lang)), id="help-title")
                yield Static("esc", id="help-esc")
            with VerticalScroll(id="help-body"):
                yield Static(_help_text(self._lang), id="help-content")
            yield Static(t("help.modal_hint", self._lang), id="help-hint")

    def action_close(self) -> None:
        self.dismiss(None)


class SelectScreen(ModalScreen[str | None]):
    """Modal option picker for model, variant, and language selection."""

    CSS = """
    SelectScreen {
        align: center middle;
    }

    #select-dialog {
        width: 76%;
        max-width: 96;
        height: auto;
        max-height: 26;
        background: $surface;
        padding: 1 2;
    }

    #select-header {
        width: 100%;
        height: 2;
    }

    #select-title {
        width: 1fr;
        text-style: bold;
    }

    #select-esc {
        width: 8;
        text-align: right;
        color: $text-muted;
    }

    #select-list {
        width: 100%;
        height: auto;
        max-height: 18;
    }

    #select-hint {
        margin-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "关闭"),
    ]

    def __init__(
        self,
        title: str,
        options: list[tuple[str, Any]],
        current: str,
        lang: str = "zh",
        on_highlight: Callable[[str], None] | None = None,
        on_delete: Callable[[str], bool] | None = None,
        on_empty: Callable[[], None] | None = None,
        hint: str | None = None,
    ):
        super().__init__()
        self._title = title
        self._options = options
        self._current = current
        self._lang = lang
        self._on_highlight = on_highlight
        self._on_delete = on_delete
        self._on_empty = on_empty
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="select-dialog"):
            with Horizontal(id="select-header"):
                yield Static(self._title, id="select-title")
                yield Static("esc", id="select-esc")
            yield OptionList(
                *[
                    Option(self._option_prompt(value, label), id=value or "auto")
                    for value, label in self._options
                ],
                id="select-list",
            )
            yield Static(self._hint or t("select.hint", self._lang), id="select-hint")

    def on_mount(self) -> None:
        option_list = self.query_one("#select-list", OptionList)
        current_id = self._current or "auto"
        for index in range(option_list.option_count):
            option = option_list.get_option_at_index(index)
            if option.id == current_id:
                option_list.highlighted = index
                break
        option_list.focus()

    @on(OptionList.OptionSelected, "#select-list")
    def on_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss("" if event.option.id == "auto" else event.option.id)

    @on(OptionList.OptionHighlighted, "#select-list")
    def on_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self._on_highlight is not None:
            self._on_highlight("" if event.option.id == "auto" else event.option.id)

    def on_key(self, event: events.Key) -> None:
        if event.key in {"up", "down"}:
            event.stop()
            event.prevent_default()
            option_list = self.query_one("#select-list", OptionList)
            if event.key == "up":
                option_list.action_cursor_up()
            else:
                option_list.action_cursor_down()
            return
        if event.key not in {"backspace", "delete"} or self._on_delete is None:
            return
        event.stop()
        event.prevent_default()
        option_list = self.query_one("#select-list", OptionList)
        highlighted = option_list.highlighted
        if highlighted is None or highlighted < 0 or highlighted >= option_list.option_count:
            return
        option = option_list.get_option_at_index(highlighted)
        value = "" if option.id == "auto" else str(option.id)
        if not value or not self._on_delete(value):
            return
        option_list.remove_option_at_index(highlighted)
        self._options = [(v, label) for v, label in self._options if v != value]
        if option_list.option_count == 0:
            self.dismiss(None)
            if self._on_empty is not None:
                self._on_empty()
            return
        option_list.highlighted = min(highlighted, option_list.option_count - 1)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _option_prompt(self, value: str, label: Any) -> Any:
        selected = "●" if (value or "auto") == (self._current or "auto") else " "
        if isinstance(label, str):
            return f"{selected} {label}"
        prompt = Table.grid(expand=True, padding=0)
        prompt.add_column(width=2, no_wrap=True)
        prompt.add_column(ratio=1, no_wrap=True)
        prompt.add_row(selected, label)
        return prompt


class ChatTextArea(TextArea):
    """Multiline chat input with Enter-to-send behavior."""

    BINDINGS = [
        Binding("enter", "submit", show=False, priority=True),
        Binding("shift+enter", "newline", show=False, priority=True),
        Binding("ctrl+j", "newline", show=False, priority=True),
        Binding("ctrl+enter", "newline", show=False, priority=True),
        *TextArea.BINDINGS,
    ]

    def on_focus(self, event: events.Focus) -> None:
        getattr(self.app, "_set_input_focus_style")(True)

    def on_blur(self, event: events.Blur) -> None:
        getattr(self.app, "_set_input_focus_style")(False)

    async def _on_key(self, event: events.Key) -> None:
        app = self.app
        cmd_list = getattr(app, "_cmd_list", None)
        commands_visible = cmd_list is not None and cmd_list.styles.display != "none"

        if commands_visible:
            if event.key == "up":
                event.stop()
                event.prevent_default()
                current_index = cmd_list.index if cmd_list.index is not None else 0
                if current_index > 0:
                    cmd_list.index = current_index - 1
                getattr(app, "_sync_command_selection")()
                return
            if event.key == "down":
                event.stop()
                event.prevent_default()
                current_index = cmd_list.index if cmd_list.index is not None else 0
                if current_index < len(cmd_list) - 1:
                    cmd_list.index = current_index + 1
                getattr(app, "_sync_command_selection")()
                return
            if event.key == "enter":
                event.stop()
                event.prevent_default()
                getattr(app, "_execute_selected_command")()
                return
            if event.key == "escape":
                event.stop()
                event.prevent_default()
                if app.screen.has_class("startup"):
                    getattr(app, "_hide_command_list")()
                else:
                    getattr(app, "_hide_command_list")(focus_input=False)
                    app.query_one("#chat-area").focus()
                return

        if event.key in ("shift+enter", "ctrl+j", "ctrl+enter"):
            event.stop()
            event.prevent_default()
            self.action_newline()
            return

        if event.key == "ctrl+c" and self.text:
            event.stop()
            event.prevent_default()
            self.clear()
            getattr(app, "_hide_command_list")(focus_input=False)
            return

        if event.key == "escape":
            event.stop()
            event.prevent_default()
            if app.screen.has_class("startup"):
                getattr(app, "_hide_command_list")()
            else:
                getattr(app, "_hide_command_list")(focus_input=False)
                app.query_one("#chat-area").focus()
            return

        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.action_submit()
            return

        await super()._on_key(event)

    def action_newline(self) -> None:
        self.insert("\n")

    def action_submit(self) -> None:
        app = self.app
        current_text = self.text.strip()
        if _command_requires_input(current_text):
            self.load_text(current_text + " ")
            self.action_cursor_line_end()
            return
        commands = app._command_candidates(current_text)
        if current_text.startswith("/"):
            exact_match = any(current_text == command for command, _ in commands)
            prefix_matches = [
                command for command, _ in commands if command.startswith(current_text)
            ]
            if not exact_match and prefix_matches:
                getattr(app, "_execute_selected_command")()
                return
        self.app.run_worker(getattr(self.app, "_submit_user_input")(), exclusive=False)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        event.prevent_default()
        getattr(self.app, "_scroll_chat")(up=True)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        event.prevent_default()
        getattr(self.app, "_scroll_chat")(up=False)


class CompactSummaryBlock(Static):
    """Distinct summary block for compacted conversation context."""

    def __init__(self, title: str, *, classes: str | None = None) -> None:
        super().__init__(classes=classes)
        self.title = title
        self.collapsed = True
        self._title_widget: Static | None = None

    DEFAULT_CSS = """
    CompactSummaryBlock {
        width: 1fr;
        height: auto;
        min-height: 3;
        border: solid $secondary;
        background: $boost;
        padding: 1 2;
        margin: 0 0 1 10;
        color: $text-muted;
    }

    CompactSummaryBlock .compact-summary-title {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
        padding: 0;
        text-style: bold italic;
        color: $text;
    }

    CompactSummaryBlock.compact-summary-collapsed .compact-summary-title {
        margin: 0;
    }

    CompactSummaryBlock Markdown.compact-summary-body {
        margin: 0;
        padding: 0;
        color: $text-muted;
    }

    CompactSummaryBlock.compact-summary-collapsed Markdown.compact-summary-body {
        display: none;
    }
    """

    def bind_title(self, widget: Static) -> None:
        self._title_widget = widget
        self.set_collapsed(True)

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.set_class(collapsed, "compact-summary-collapsed")
        marker = "▸" if collapsed else "▾"
        if self._title_widget is not None:
            self._title_widget.update(f"{marker} {escape(self.title)}")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.set_collapsed(not self.collapsed)


class ChatScrollBarRender(ScrollBarRender):
    """Scrollbar renderer that avoids font-dependent fractional block glyphs."""

    VERTICAL_BARS = [" "] * 8


class ChatScroll(VerticalScroll):
    """Chat history scroller with explicit wheel forwarding."""

    def on_mount(self) -> None:
        self.vertical_scrollbar.renderer = ChatScrollBarRender

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        event.prevent_default()
        getattr(self.app, "_scroll_chat")(up=True)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        event.prevent_default()
        getattr(self.app, "_scroll_chat")(up=False)


class ChatMarkdownBulletList(MarkdownBulletList):
    """Bullet list that highlights restore-protection checkpoint rows."""

    def compose(self) -> ComposeResult:
        for block in self._blocks:
            if not isinstance(block, MarkdownListItem):
                continue
            bullet = MarkdownBullet()
            bullet.symbol = block.bullet
            is_safety = any(
                "恢复前安全检查点：" in child._content.plain
                or "恢复保护 ·" in child._content.plain
                for child in block._blocks
            )
            yield Horizontal(
                bullet,
                Vertical(*block._blocks),
                classes="checkpoint-safety" if is_safety else None,
            )
        self._blocks.clear()


class ChatMarkdown(Markdown):
    """Markdown body that lets chat history own wheel scrolling."""

    BLOCKS = {
        **Markdown.BLOCKS,
        "bullet_list_open": ChatMarkdownBulletList,
    }

    def __init__(self, *args, force_scroll_on_ready: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.force_scroll_on_ready = force_scroll_on_ready

    def update(self, markdown: str) -> object:
        return super().update(_render_terminal_math(markdown))

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        event.prevent_default()
        getattr(self.app, "_scroll_chat")(up=True)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        event.prevent_default()
        getattr(self.app, "_scroll_chat")(up=False)


class InlineImageAttachment(Vertical):
    """Cached chat image preview with optional terminal-protocol rendering."""

    def __init__(self, path: Path, index: int, *, classes: str | None = None) -> None:
        super().__init__(classes=classes or "image-attachment")
        self.path = path
        self.index = index
        self.collapsed = False
        self._caption_widget: Static | None = None
        width, height, size_text = image_metadata(self.path)
        dimensions = f"{width}x{height}" if width and height else "unknown"
        self._caption = f"{self.index}. {self.path.name} ({dimensions}, {size_text})"

    def compose(self) -> ComposeResult:
        self._caption_widget = Static(classes="image-caption")
        yield self._caption_widget

        if TerminalImage is not None and self.path.suffix.lower() != ".svg":
            try:
                yield TerminalImage(
                    str(terminal_image_preview(self.path)),
                    classes="terminal-image",
                )
                return
            except Exception:
                debug_log("tui.inline_image_widget_failed", path=str(self.path))

        if self.path.suffix.lower() != ".svg":
            try:
                yield Static(
                    terminal_half_block_preview(self.path),
                    classes="image-fallback-preview",
                )
            except Exception:
                debug_log("tui.inline_image_half_block_failed", path=str(self.path))

        yield Static(
            f"{self.path}\n使用 /preview {self.index} 在系统图片查看器中打开。",
            classes="image-fallback",
        )

    def on_mount(self) -> None:
        self.set_collapsed(False)

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.set_class(collapsed, "image-collapsed")
        marker = "▸" if collapsed else "▾"
        if self._caption_widget is not None:
            self._caption_widget.update(f"{marker} {escape(self._caption)}")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.set_collapsed(not self.collapsed)
        maybe_scroll = getattr(self.app, "_maybe_scroll_to_end", None)
        if maybe_scroll is not None:
            maybe_scroll()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        event.prevent_default()
        getattr(self.app, "_scroll_chat")(up=True)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        event.prevent_default()
        getattr(self.app, "_scroll_chat")(up=False)


class StatusPanel(Static):
    """Running log panel."""

    def __init__(self, index: int, *, classes: str | None = None) -> None:
        super().__init__(classes=classes)
        self.index = index
        self.lines: list[str] = []
        self.collapsed = False
        self.done = False
        self.user_expanded = False

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed

    def on_click(self, event: events.Click) -> None:
        event.stop()
        getattr(self.app, "_toggle_status_panel")(self)


@dataclass
class AgentRunState:
    agent: Any
    agent_msg: Markdown
    user_text: str
    base_messages: list[Message]
    phase: str = "thinking"
    background_task: SubAgentTask | None = None


class AeroApp(App):
    """Aero Textual TUI."""

    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        layout: vertical;
        layers: base overlay;
    }

    ToastRack {
        dock: top;
        align: right top;
    }

    #chat-area {
        height: 1fr;
        padding: 1 5 1 2;
        scrollbar-size: 1 1;
    }

    .startup #chat-area {
        display: none;
    }

    #input-panel {
        height: auto;
        width: 100%;
        align-horizontal: center;
    }

    #input-row {
        width: 100%;
        height: auto;
        align: center middle;
    }

    #command-row {
        display: none;
        position: absolute;
        layer: overlay;
        width: 100%;
        max-height: 7;
        align: center middle;
    }

    .startup #input-panel {
        height: 1fr;
        align-horizontal: center;
        align-vertical: middle;
        align: center middle;
    }

    #input-box {
        width: 88%;
        height: 5;
        border: solid $primary-darken-2;
        background: $surface;
    }

    #input-box.input-active {
        border: solid $accent;
    }

    #input-box.mode-plan {
        border: solid #8a5a16;
    }

    #input-box.mode-plan.input-active {
        border: solid #f39c12;
    }

    #input-box.mode-execute {
        border: solid #2f6187;
    }

    #input-box.mode-execute.input-active {
        border: solid #5dade2;
    }

    #input-box.mode-qa {
        border: solid #2e7d52;
    }

    #input-box.mode-qa.input-active {
        border: solid #58d68d;
    }

    #mode-label {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-align: right;
        text-style: italic;
    }

    .startup #input-box {
        width: 58%;
    }

    .confirming #input-box {
        opacity: 0%;
    }

    #user-input {
        width: 100%;
        height: 2;
        padding: 0 1;
        border: none;
        background: $surface;
        scrollbar-size: 0 0;
    }

    #input-meta {
        width: 1fr;
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }

    #input-meta-row {
        width: 100%;
        height: 1;
    }

    #startup-logo {
        display: none;
        width: 100%;
        height: 1;
        content-align: center middle;
        color: $text-muted;
        text-style: bold;
        margin-bottom: 2;
    }

    .startup #startup-logo {
        display: block;
    }

    .message-row {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
    }

    .message-label {
        width: 10;
        min-width: 10;
        height: auto;
        padding-right: 3;
        text-align: right;
        text-style: bold;
    }

    .message-body {
        width: 1fr;
        height: auto;
        margin: 0;
        padding: 0;
    }

    .user-label {
        color: $warning;
    }

    .agent-label {
        color: $accent;
    }

    .user-content {
        color: $text;
        text-style: bold;
    }

    Markdown.agent-content {
        padding: 0;
    }

    .agent-stack {
        width: 1fr;
        height: auto;
    }

    .agent-content {
        color: $text-muted;
    }

    Markdown.agent-content .checkpoint-safety {
        color: $warning;
    }

    .image-attachment {
        width: 100%;
        height: auto;
        margin: 1 0 0 0;
        padding: 0;
        border-left: solid $primary;
        color: $text-muted;
    }

    .image-caption {
        width: 100%;
        height: 1;
        padding: 0 0 0 2;
        color: $text-muted;
        text-style: bold;
    }

    .image-attachment.image-collapsed .terminal-image,
    .image-attachment.image-collapsed .image-fallback-preview,
    .image-attachment.image-collapsed .image-fallback {
        display: none;
    }

    .terminal-image {
        width: auto;
        height: 30;
        margin: 1 0 0 2;
    }

    .image-fallback {
        width: 100%;
        padding: 0 2;
        color: $text-muted;
    }

    .image-fallback-preview {
        width: auto;
        margin: 1 0 1 2;
        color: $text-muted;
    }

    .welcome-line {
        width: 100%;
        content-align: center middle;
    }

    .status-panel {
        margin: 0 0 1 10;
        padding: 0 1;
        border-left: solid gray;
        color: $text-muted;
        text-style: italic;
        opacity: 85%;
    }

    .status-collapsed {
        height: 1;
    }

    .divider {
        margin: 0 0 1 10;
        color: $text-disabled;
    }

    .help-block {
        margin: 0 0 2 0;
    }

    #command-list {
        display: none;
        width: 88%;
        max-height: 7;
        border: solid $primary;
        background: $surface;
    }

    .startup #command-list {
        width: 58%;
    }

    #command-list ListItem {
        padding: 0 1;
    }

    #command-list .command-option {
        width: 100%;
        padding: 0 1;
        background: $surface;
        color: $text;
    }

    #command-list .command-option-selected {
        background: $primary;
        color: $background;
        text-style: bold;
    }

    #command-list ListItem.--highlight {
        background: $primary;
        color: $background;
    }

    #command-list ListItem.command-selected {
        background: $primary;
        color: $background;
        text-style: bold;
    }

    #footer-bar {
        height: 1;
        width: 100%;
        background: $boost;
        color: $text-muted;
    }

    #footer-status {
        width: 1fr;
        content-align: left middle;
        color: $text;
        padding: 0 1;
    }

    """

    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+s", "save_log", "保存日志"),
        ("ctrl+y", "copy_last_reply", "复制回复"),
        Binding("tab", "cycle_mode", "切换模式", show=False),
    ]

    def __init__(
        self,
        config: AeroConfig,
        persist_config: bool = True,
        resume_last_session: bool = False,
    ):
        super().__init__()
        self.config = config
        self.persist_config = persist_config
        self._resume_last_session = resume_last_session
        self.agent = None
        self.last_error = ""
        self._agent_msg: Markdown | None = None
        self._status_msg: StatusPanel | None = None
        self._status_sessions: list[StatusPanel] = []
        self._agent_worker: Worker | None = None
        self._main_run_state: AgentRunState | None = None
        self._last_escape_at = 0.0
        self._last_reply_text: str = ""
        self._chat_log: list[str] = []
        self._pending_llm_setup: dict | None = None
        self._pending_credential_scope: str | None = None
        self._secret_handles: dict[str, str] = {}
        self._filtered_commands: list[tuple[str, str]] = []
        self._cmd_list: ListView | None = None
        self._model_info: Static | None = None
        self._input_meta: Static | None = None
        self._footer_status: Static | None = None
        self._footer_status_token = 0
        self._footer_temp_text = ""
        self._official_available_credits: str | None = None
        self._subagent_notice_until = 0.0
        self._subagent_footer_active = False
        self._subagent_footer_frame = 0
        self._subagents = SubAgentManager()
        self._subagent_workers: dict[str, Worker] = {}
        self._pending_subagent_context_notes: list[str] = []
        self._theme_before_select: str | None = None
        self._activity_running = False
        self._activity_frame = 0
        self._thinking_frame = 0
        self._chat_started = False
        self._commands_list: list[tuple[str, str]] = []
        self._secondary_commands_list: list[tuple[str, str]] = []
        self._image_attachments: list[Path] = []
        self._session_mgr = None  # Lazy init
        self._checkpoint_mgr = None  # Lazy init
        self._experiment_mgr = None  # Lazy init
        self._experiment_session_mgr = None  # Lazy init
        self._experiment_finish_worker: Worker | None = None
        self._project_dir = Path.cwd().resolve()
        self._session_id: str | None = None
        self._session_saved_on_exit = False
        self._session_title_workers: set[str] = set()
        self._pending_session_title: str = ""
        self._deferred_subagent_notices: list = []
        self._streaming_text = False
        self._queued_message: str | None = None
        self._user_scrolled_up = False
        saved_theme = _load_saved_theme()
        if saved_theme:
            theme = _resolve_theme_name(saved_theme, self.available_themes)
            if theme is not None:
                self.theme = theme

    def compose(self) -> ComposeResult:
        lang = self.config.language
        yield ChatScroll(id="chat-area")
        with Horizontal(id="command-row"):
            yield ListView(id="command-list")
        with Vertical(id="input-panel"):
            yield Static(STARTUP_LOGO, id="startup-logo")
            with Horizontal(id="input-row"):
                with Vertical(id="input-box"):
                    yield ChatTextArea(id="user-input", show_line_numbers=False)
                    with Horizontal(id="input-meta-row"):
                        yield Static("", id="input-meta")
                        yield Static("", id="mode-label")
        with Horizontal(id="footer-bar"):
            yield Static("", id="footer-status")

    def on_mount(self) -> None:
        debug_log("tui.mount", language=self.config.language)
        self._init_agent()
        self.screen.add_class("startup")

        lang = self.config.language
        chat = self.query_one("#chat-area", VerticalScroll)
        self._mount_chat_title()
        self._chat_log = [
            t("app.title", lang),
            self._model_status_text(markup=False),
            "",
        ]

        self.sub_title = t("app.ready", lang)
        self.query_one("#user-input", TextArea).focus()

        self._cmd_list = self.query_one("#command-list", ListView)
        self._cmd_list.styles.display = "none"
        self._input_meta = self.query_one("#input-meta", Static)
        self._refresh_model_info()
        self._refresh_mode_ui()
        self._footer_status = self.query_one("#footer-status", Static)
        self._refresh_commands()
        self._set_input_focus_style(True)
        if self.config.llm.provider == "official":
            self.run_worker(self._refresh_official_credits(), exclusive=False)
        active_experiment = self._get_experiment_mgr().active()
        if active_experiment is not None:
            self._load_experiment_slot(active_experiment["id"])
            self._set_footer_status(f"当前实验：{active_experiment['name']}")
        elif self._resume_last_session:
            self._resume_latest_project_session()

        if self.persist_config and _config_needs_llm_setup(self.config):
            self.call_after_refresh(self._open_first_run_setup)

    def _open_first_run_setup(self) -> None:
        from aero.cli.setup_wizard import FirstRunSetupScreen

        self.push_screen(FirstRunSetupScreen(), self._complete_first_run_setup)

    def _complete_first_run_setup(self, result: dict | None) -> None:
        if result is None:
            self.exit(message="未完成主模型配置，已退出。")
            return
        primary = result["primary"]
        self.config.llm.provider = str(primary["provider"])
        self.config.llm.model = str(primary["model"])
        self.config.llm.base_url = str(primary["base_url"])
        self.config.llm.supports_vision = bool(result["primary_supports_vision"])
        self.config.llm.set_active_api_key(str(primary["api_key"]))
        save_llm_profile(
            self.config.llm.provider,
            str(primary["api_key"]),
            self.config.llm.model,
            self.config.llm.base_url,
        )

        vision = result["vision"]
        self.config.vision.mode = str(vision["mode"])
        if self.config.vision.mode == "separate":
            self.config.vision.provider = str(vision["provider"])
            self.config.vision.model = str(vision["model"])
            self.config.vision.base_url = str(vision["base_url"])
            self.config.vision.api_key = str(vision["api_key"])
            from aero.core.config import save_vision_api_key

            save_vision_api_key(
                self.config.vision.api_key,
                self.config.vision.base_url,
                provider=self.config.vision.provider,
                model=self.config.vision.model,
                mode="separate",
            )
        elif self.config.vision.mode == "reuse_primary":
            self._reuse_primary_vision_model(persist=False)

        web_search = result.get("web_search", {})
        if web_search.get("configured"):
            ws_api_key = web_search.get("api_key", "").strip()
            if ws_api_key:
                self.config.web_search.provider = str(web_search.get("provider", "bailian"))
                self.config.web_search.api_key = ws_api_key
                self.config.web_search.base_url = str(
                    web_search.get(
                        "base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"
                    )
                )
                self.config.web_search.model = str(web_search.get("model", "qwen-turbo"))
                self.config.web_search.enabled = True
                self.config.web_search.mcp_verified = bool(web_search.get("mcp_verified", True))
                self.config.web_search.mcp_verified_provider = self.config.web_search.provider
                self.config.web_search.mcp_verified_at = time.time()
                save_web_search_api_key(
                    ws_api_key,
                    self.config.web_search.base_url,
                    provider=self.config.web_search.provider,
                    model=self.config.web_search.model,
                )
                save_web_search_state(
                    enabled=True,
                    mcp_verified=self.config.web_search.mcp_verified,
                    mcp_verified_provider=self.config.web_search.provider,
                    mcp_verified_at=self.config.web_search.mcp_verified_at,
                )
        else:
            # “暂不配置” is an explicit choice.  Do not inherit a stale
            # user-level enabled flag or reuse the primary/vision key as a
            # search credential on this initialization.
            self.config.web_search.enabled = False
            self.config.web_search.api_key = ""
            self.config.web_search.mcp_verified = False
            self.config.web_search.mcp_verified_provider = ""
            self.config.web_search.mcp_verified_at = 0.0
            save_web_search_state(
                enabled=False,
                mcp_verified=False,
                mcp_verified_provider="",
                mcp_verified_at=0.0,
            )

        _save_config(self.config)
        self._init_agent()
        self._chat_log = [
            t("app.title", self.config.language),
            self._model_status_text(markup=False),
            "",
        ]
        self._refresh_model_info()
        self._set_footer_status("主模型已配置，可以开始对话。")

    def _open_provider_setup(self, previous_provider: str) -> None:
        from aero.cli.setup_wizard import FirstRunSetupScreen

        primary = {
            "provider": self.config.llm.provider,
            "model": self.config.llm.model,
            "base_url": self.config.llm.base_url,
        }
        self.push_screen(
            FirstRunSetupScreen(primary_only=True, primary=primary),
            lambda result: self._complete_provider_setup(result, previous_provider),
        )

    def _complete_provider_setup(
        self,
        result: dict | None,
        previous_provider: str,
    ) -> None:
        if result is None:
            self.config.llm.switch_provider(previous_provider)
            self._sync_agent_llm_config()
            if self.persist_config:
                _save_config(self.config)
            self._refresh_model_info()
            self._set_footer_status("已取消配置，恢复原模型服务商。")
            return

        primary = result["primary"]
        self.config.llm.provider = str(primary["provider"])
        self.config.llm.model = str(primary["model"])
        self.config.llm.base_url = str(primary["base_url"])
        self.config.llm.supports_vision = bool(result["primary_supports_vision"])
        self.config.llm.set_active_api_key(str(primary["api_key"]))
        self.config.llm.apply_active_provider_defaults()
        save_llm_profile(
            self.config.llm.provider,
            str(primary["api_key"]),
            self.config.llm.model,
            self.config.llm.base_url,
        )
        if self.persist_config:
            _save_config(self.config)
        self._sync_agent_llm_config()
        self._refresh_model_info()
        self._set_footer_status(
            f"{_display_provider_name(self.config.llm.provider)} 已配置并切换。"
        )

    def _sync_agent_llm_config(self) -> None:
        if self.agent is None:
            return
        self.agent.llm.config.provider = self.config.llm.provider
        self.agent.llm.config.base_url = self.config.llm.base_url
        self.agent.llm.config.model = self.config.llm.model
        self.agent.llm.config.api_key = self.config.llm.active_api_key()
        self.agent.config.llm.provider = self.config.llm.provider
        self.agent.config.llm.base_url = self.config.llm.base_url
        self.agent.config.llm.model = self.config.llm.model
        self.agent.config.llm.set_active_api_key(self.config.llm.active_api_key())

    async def _open_vision_setup_for_current_task(self) -> bool:
        from aero.cli.setup_wizard import FirstRunSetupScreen

        result = await self.push_screen_wait(
            FirstRunSetupScreen(
                vision_only=True,
                primary_supports_vision=self.config.llm.supports_vision,
            )
        )
        if result is None or result["vision"]["mode"] == "unconfigured":
            return False
        vision = result["vision"]
        self.config.vision.mode = str(vision["mode"])
        if self.config.vision.mode == "separate":
            self.config.vision.provider = str(vision["provider"])
            self.config.vision.model = str(vision["model"])
            self.config.vision.base_url = str(vision["base_url"])
            self.config.vision.api_key = str(vision["api_key"])
            from aero.core.config import save_vision_api_key

            save_vision_api_key(
                self.config.vision.api_key,
                self.config.vision.base_url,
                provider=self.config.vision.provider,
                model=self.config.vision.model,
                mode="separate",
            )
        elif self.config.vision.mode == "reuse_primary":
            self._reuse_primary_vision_model(persist=False)
        _save_config(self.config)
        self._set_footer_status("视觉能力已配置，正在继续当前任务。")
        return True

    def _set_input_focus_style(self, active: bool) -> None:
        input_box = self.query_one("#input-box")
        input_box.set_class(active, "input-active")

    def on_mouse_down(self, event: events.MouseDown) -> None:
        input_box = self.query_one("#input-box")
        if input_box.region.contains(event.screen_x, event.screen_y):
            self.call_later(self.query_one("#user-input", TextArea).focus)

    def on_text_selected(self, event: events.TextSelected) -> None:
        try:
            selected_text = self.screen.get_selected_text()
        except IndexError:
            # Textual keeps the screen-level selection briefly after a status
            # panel re-renders from many log lines to its one-line collapsed
            # form.  The stale offsets can then be outside that widget's new
            # content.  Ignore that invalid selection instead of letting the
            # UI event crash the entire chat process.
            self.screen.clear_selection()
            event.stop()
            return
        if not selected_text:
            return
        event.stop()
        try:
            self._copy_text_to_clipboard(selected_text)
        except Exception as exc:
            self.notify(f"复制失败: {exc}", severity="error", timeout=3)
            return
        self.notify("已复制选中文字", timeout=1.5)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        event.prevent_default()
        self._scroll_chat(up=True)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        event.prevent_default()
        self._scroll_chat(up=False)

    def _scroll_chat(self, *, up: bool) -> None:
        if self.screen.has_class("startup"):
            return
        if up:
            self._user_scrolled_up = True
        delta = -5 if up else 5
        self.query_one("#chat-area", VerticalScroll).scroll_relative(
            y=delta,
            animate=False,
            force=True,
            immediate=True,
        )

    def _maybe_scroll_to_end(self) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        if not self._user_scrolled_up or chat.is_vertical_scroll_end:
            chat.scroll_end(animate=False)
            self._user_scrolled_up = False

    def _scroll_chat_to_end(self) -> None:
        self._maybe_scroll_to_end()

    def _force_scroll_chat_to_end(self) -> None:
        """Scroll after dynamic Markdown has finished changing the layout height."""
        self._user_scrolled_up = False
        chat = self.query_one("#chat-area", VerticalScroll)
        chat.scroll_end(
            animate=False,
            force=True,
            immediate=True,
        )

    def on_markdown_table_of_contents_updated(
        self, event: Markdown.TableOfContentsUpdated
    ) -> None:
        markdown = event.markdown
        if not isinstance(markdown, ChatMarkdown) or not markdown.force_scroll_on_ready:
            return
        markdown.force_scroll_on_ready = False
        self._force_scroll_chat_to_end()
        self.call_after_refresh(self._force_scroll_chat_to_end)

    def _mount_compact_summary_block(self, summary_text: str) -> CompactSummaryBlock:
        chat = self.query_one("#chat-area", VerticalScroll)
        block = CompactSummaryBlock(
            t("app.compact_summary_title", self.config.language),
            classes="compact-summary",
        )
        chat.mount(block)
        title = Static("", classes="compact-summary-title")
        block.mount(title)
        block.mount(ChatMarkdown(summary_text, classes="compact-summary-body"))
        block.bind_title(title)
        return block

    def _strike_last_user_message(self) -> None:
        if not hasattr(self, "_last_user_text"):
            return
        text = self._last_user_text
        chat = self.query_one("#chat-area", VerticalScroll)
        for row in reversed(chat.query(".user-message")):
            body = row.query_one(".user-content")
            body.update(f"[s]{escape(text)}[/s]")
            return

    def _handoff_main_run_to_background(self) -> None:
        state = self._main_run_state
        if state is None or state.background_task is not None:
            return
        title = _subagent_title_from_text(state.user_text)
        task = self._subagents.create(
            title=title,
            description=state.user_text,
            success_criteria="完成当前已开始的任务，并报告关键结果和产物路径。",
            context_summary="这是用户继续发言时从主对话转交到后台的任务。",
        )
        task.agent = state.agent
        state.background_task = task
        if self._agent_worker is not None:
            self._subagent_workers[task.id] = self._agent_worker
        try:
            state.agent_msg.update("*已转交后台任务。*")
        except Exception:
            pass
        if self._status_msg is not None:
            self._append_status("已转交后台处理")
            self._render_status_expanded(self._status_msg, limit=8)
            self._collapse_status(done=False)
        self._show_subagent_handoff_notice()
        self.notify(f"已转交后台任务：{task.title}", timeout=3)

        from aero.agent.loop import AgentLoop

        new_agent = AgentLoop(self.config)
        new_agent.messages = copy.deepcopy(state.base_messages)
        new_agent.always_allow = set(getattr(state.agent, "always_allow", set()))
        new_agent.tracker = state.agent.tracker
        self.agent = new_agent
        self._agent_worker = None
        self._main_run_state = None
        self._status_msg = None

    def _enter_chat_mode(self) -> None:
        if self._chat_started:
            return
        self._chat_started = True
        self.screen.remove_class("startup")
        self.query_one("#user-input", TextArea).focus()

    def _mount_chat_title(self) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        chat.mount(Static(t("app.title", self.config.language), classes="welcome-line"))
        chat.mount(Static(""))

    def _reset_chat_log_with_title(self) -> None:
        self._chat_log = [
            t("app.title", self.config.language),
            self._model_status_text(markup=False),
            "",
        ]

    def _chat_transcript(self) -> list[dict[str, str]]:
        """Return display-only history without adding it to the LLM context."""
        transcript: list[dict[str, str]] = []
        prefixes = (("你:\n", "user"), ("Aero:\n", "assistant"))
        for entry in self._chat_log:
            for prefix, role in prefixes:
                if entry.startswith(prefix):
                    transcript.append({"role": role, "content": entry[len(prefix) :]})
                    break
            else:
                compact_prefix = "[上下文压缩总结]\n"
                if entry.startswith(compact_prefix):
                    transcript.append(
                        {"role": "summary", "content": entry[len(compact_prefix) :]}
                    )
        return transcript

    def _ready_subtitle(self) -> str:
        return t("app.ready", self.config.language)

    def _model_status_text(self, markup: bool = True) -> str:
        lang = self.config.language
        text = (
            t("app.model_line", lang).format(
                provider=self.config.llm.provider,
                model=self.config.llm.model,
            )
            + t("app.effort_line", lang).format(
                effort=self.config.llm.reasoning_effort or "auto",
            )
        )
        vision_config = resolved_vision_config(self.config)
        if vision_config is not None and not self._vision_matches_primary(vision_config):
            text += t("app.vision_line", lang).format(
                model=_display_vision_model_name(vision_config.model),
                provider=_display_vision_provider_name(vision_config.provider),
            )
        from aero.core.official_account import load_official_session

        official = load_official_session()
        if official.is_logged_in:
            text += f"\n官方账户：已登录 {official.email or official.user_id}"
        return f"[dim]{text}[/dim]" if markup else text

    def _input_meta_text(self) -> str:
        model_name = _display_model_name(self.config.llm.model)
        provider = _display_provider_name(self.config.llm.provider)
        effort = self.config.llm.reasoning_effort or "auto"
        text = (
            f"{escape(model_name)} [dim]{escape(provider)} ·[/dim] "
            f"[bold warning]{escape(effort)}[/bold warning]"
        )
        vision_config = resolved_vision_config(self.config)
        if vision_config is not None and not self._vision_matches_primary(vision_config):
            vision_model = _display_vision_model_name(vision_config.model)
            vision_provider = _display_vision_provider_name(vision_config.provider)
            text += (
                f" [dim]&[/dim] {escape(vision_model)} "
                f"[dim]{escape(vision_provider)}[/dim]"
            )
        if self.agent is not None and self.agent.tracker.total_tokens > 0:
            text += _usage_meta_text(
                self.agent.tracker,
                self.config.llm.model,
                include_cost=self.config.llm.provider != "official",
            )
        if (
            self.config.llm.provider == "official"
            and self._official_available_credits is not None
        ):
            text += (
                " [dim]|[/dim] [dim]剩余额度[/dim] "
                f"{escape(self._official_available_credits)} credits"
            )
        return text

    def _vision_matches_primary(self, vision_config: Any) -> bool:
        """Whether the resolved visual endpoint is exactly the active LLM."""
        return (
            normalize_provider_id(vision_config.provider)
            == normalize_provider_id(self.config.llm.provider)
            and vision_config.model.strip().lower()
            == self.config.llm.model.strip().lower()
        )

    _COMMANDS = None  # Replaced by instance-level _commands_list for i18n

    def on_key(self, event: events.Key) -> None:
        if event.key == "tab":
            event.stop()
            event.prevent_default()
            self.action_cycle_mode()
            return

        cmd_list = self._cmd_list
        if cmd_list is not None and cmd_list.styles.display != "none":
            if event.key == "up":
                event.stop()
                current_index = cmd_list.index if cmd_list.index is not None else 0
                if current_index > 0:
                    cmd_list.index = current_index - 1
                self._sync_command_selection()
                return
            if event.key == "down":
                event.stop()
                current_index = cmd_list.index if cmd_list.index is not None else 0
                if current_index < len(cmd_list) - 1:
                    cmd_list.index = current_index + 1
                self._sync_command_selection()
                return
            if event.key == "enter":
                event.stop()
                self._execute_selected_command()
                return
            if event.key == "escape":
                event.stop()
                self._hide_command_list()
                return

        input_area = self.query_one("#user-input", TextArea)
        if self.focused == input_area:
            if event.key in ("shift+enter", "ctrl+j", "ctrl+enter"):
                event.stop()
                input_area.insert("\n")
                return
            if event.key == "enter":
                event.stop()
                self.run_worker(self._submit_user_input(), exclusive=False)
                return
            if event.key in ("up", "down"):
                return

        chat = self.query_one("#chat-area", VerticalScroll)
        if event.key == "enter":
            event.stop()
            input_area.focus()
            return
        if event.key == "up":
            event.stop()
            self._user_scrolled_up = True
            chat.scroll_up(animate=False)
            return
        if event.key == "down":
            event.stop()
            chat.scroll_down(animate=False)
            return
        if event.key == "pageup":
            event.stop()
            self._user_scrolled_up = True
            chat.scroll_page_up(animate=False)
            return
        if event.key == "pagedown":
            event.stop()
            chat.scroll_page_down(animate=False)
            return

        if event.key != "escape":
            return
        now = time.monotonic()
        if now - self._last_escape_at <= 1.0:
            event.stop()
            self._last_escape_at = 0.0
            self._cancel_agent_run()
            return
        self._last_escape_at = now

    async def _submit_user_input(self) -> None:
        input_area = self.query_one("#user-input", TextArea)
        text = input_area.text.strip()
        input_area.clear()
        if text:
            await self._process(text)

    @on(TextArea.Changed, "#user-input")
    async def on_input_changed(self, event: TextArea.Changed) -> None:
        value = event.text_area.text.strip()
        if value.startswith("/"):
            await self._populate_command_list(value)
        else:
            self._hide_command_list(focus_input=False)

    async def _populate_command_list(self, prefix: str) -> None:
        cmd_list = self._cmd_list
        if cmd_list is None:
            return
        await cmd_list.clear()
        matched = _command_suggestions(
            prefix,
            self._commands_list,
            self._secondary_commands_list,
        )
        self._filtered_commands = []
        for cmd, desc in matched:
            selected = len(self._filtered_commands) == 0
            self._filtered_commands.append((cmd, desc))
            option = Static(
                self._command_item_text(cmd, desc, selected),
                classes="command-option",
            )
            option.set_class(selected, "command-option-selected")
            item = ListItem(option)
            item.set_class(selected, "command-selected")
            await cmd_list.append(item)
        if self._filtered_commands:
            self._resize_command_list()
            cmd_list.styles.display = "block"
            self.query_one("#command-row").styles.display = "block"
            cmd_list.styles.display = "block"
            cmd_list.index = 0
            self._sync_command_selection()
            cmd_list.scroll_home(animate=False)
            self._position_command_list()
        else:
            self._hide_command_list(focus_input=False)

    def _sync_command_selection(self) -> None:
        cmd_list = self._cmd_list
        if cmd_list is None:
            return
        selected = cmd_list.index if cmd_list.index is not None else 0
        if self._filtered_commands and (selected < 0 or selected >= len(self._filtered_commands)):
            selected = 0
            cmd_list.index = selected
        for index, item in enumerate(cmd_list.children):
            if isinstance(item, ListItem):
                is_selected = index == selected
                item.set_class(is_selected, "command-selected")
                if index < len(self._filtered_commands):
                    cmd, desc = self._filtered_commands[index]
                    try:
                        option = item.query_one(Static)
                        option.set_class(is_selected, "command-option-selected")
                        option.update(self._command_item_text(cmd, desc, is_selected))
                    except Exception:
                        pass

    def _command_item_text(self, cmd: str, desc: str, selected: bool) -> str:
        if selected:
            return f"[bold]{escape(cmd)}[/bold]  {escape(desc)}"
        return f"[bold]{escape(cmd)}[/bold]  [dim]{escape(desc)}[/dim]"

    def _command_candidates(self, prefix: str) -> list[tuple[str, str]]:
        return _command_suggestions(
            prefix,
            self._commands_list,
            self._secondary_commands_list,
        )

    def _command_list_height(self) -> int:
        if not self._filtered_commands:
            return 0
        return min(len(self._filtered_commands) + 2, 7)

    def _resize_command_list(self) -> None:
        cmd_list = self._cmd_list
        if cmd_list is None:
            return
        height = self._command_list_height()
        if height <= 0:
            return
        command_row = self.query_one("#command-row")
        cmd_list.styles.height = height
        command_row.styles.height = height

    def _position_command_list(self) -> None:
        try:
            input_box = self.query_one("#input-box")
            command_row = self.query_one("#command-row")
        except Exception:
            return
        y = max(0, input_box.region.y - self._command_list_height())
        command_row.styles.offset = Offset(0, y)

    def action_quit(self) -> None:
        self._save_session_on_exit()
        self._do_cleanup_exit()

    def _do_cleanup_exit(self) -> None:
        worker_running = self._agent_worker is not None and self._agent_worker.is_running
        if self.agent is not None:
            self.agent.cancel()
        if self._agent_worker is not None and self._agent_worker.is_running:
            self._agent_worker.cancel()
        for task in self._subagents.list():
            self._subagents.cancel(task.id)
        subagent_running = any(worker.is_running for worker in self._subagent_workers.values())
        for worker in self._subagent_workers.values():
            if worker.is_running:
                worker.cancel()
        if worker_running or subagent_running:
            timer = threading.Timer(2.0, lambda: os._exit(0))
            timer.daemon = True
            timer.start()
        self.exit()

    def on_unmount(self) -> None:
        self._save_session_on_exit()

    def _save_session_on_exit(self) -> None:
        if self._session_saved_on_exit:
            return
        self._session_saved_on_exit = True
        try:
            self._auto_save_session()
        except Exception as e:
            debug_log("tui.session_exit_save_failed", error=str(e))

    def action_save_log(self) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        lines = self._chat_log or self._collect_log_lines(chat)
        text = "\n".join(lines)
        save_dir = Path.home() / ".aero"
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / "chat.log"
        save_path.write_text(text)
        self.notify(f"已保存到 {save_path}", timeout=3)

    def _collect_log_lines(self, widget) -> list[str]:
        lines = []
        for child in widget.children:
            content = ""
            if isinstance(child, Markdown):
                content = str(getattr(child, "_markdown", "") or "")
            elif hasattr(child, "_renderable") and child._renderable is not None:
                content = str(child._renderable)
            if content:
                content = (
                    content.replace("[dim bold]", "").replace("[/dim bold]", "")
                    .replace("[bold cyan]", "").replace("[/bold cyan]", "")
                    .replace("[dim]", "").replace("[/dim]", "")
                    .replace("[error]", "").replace("[/error]", "")
                )
                lines.append(content)
            if child.children:
                lines.extend(self._collect_log_lines(child))
        return lines

    async def _process(self, text: str) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        debug_log(
            "tui.process",
            text_length=len(text),
            command=text.split(maxsplit=1)[0] if text.startswith("/") else None,
        )

        if text == "/quit":
            self._save_session_on_exit()
            self._do_cleanup_exit()
            return

        if text == "/clear":
            lang = self.config.language
            chat.remove_children()
            if self.agent:
                self.agent.messages = [self.agent.messages[0]]
                self.agent.tracker = TokenTracker()
            self._last_reply_text = ""
            self._status_msg = None
            self._status_sessions = []
            self._image_attachments = []
            self._chat_log = [t("app.conversation_cleared", lang)]
            self._session_id = None
            set_session_id(None)
            chat.mount(
                Static(
                    f"[dim]{t('app.conversation_cleared', lang)}[/dim]",
                    classes="divider",
                )
            )
            self._hide_command_list()
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/new" or text.startswith("/new "):
            self._handle_new_session_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/copy":
            self.action_copy_last_reply()
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/preview" or text.startswith("/preview "):
            self._handle_preview_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/model" or text.startswith("/model "):
            self._handle_model_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/provider" or text.startswith("/provider "):
            self._handle_provider_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/login" and OFFICIAL_ACCOUNT_UI_ENABLED:
            await self._handle_official_login()
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/logout" and OFFICIAL_ACCOUNT_UI_ENABLED:
            await self._handle_official_logout()
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/account" and OFFICIAL_ACCOUNT_UI_ENABLED:
            await self._handle_official_account()
            self.query_one("#user-input", TextArea).focus()
            return

        if text in {"/login", "/logout", "/account"}:
            self._set_footer_status("当前版本暂未开放官方账户功能。")
            self.query_one("#user-input", TextArea).focus()
            return

        if (
            text == "/variants"
            or text.startswith("/variants ")
            or text == "/effort"
            or text.startswith("/effort ")
        ):
            self._handle_variants_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/language" or text.startswith("/language "):
            self._handle_language_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/theme" or text.startswith("/theme "):
            self._handle_theme_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/vision" or text.startswith("/vision "):
            self._handle_vision_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/websearch" or text.startswith("/websearch "):
            await self._handle_websearch_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text in {"/bill", "/billing"}:
            await self._handle_bill_command()
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/mode" or text.startswith("/mode "):
            self._handle_mode_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/help" or text == "help":
            self._handle_help()
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/set" or text.startswith("/set "):
            self._handle_set_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/revoke" or text.startswith("/revoke "):
            self._handle_revoke(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/session" or text.startswith("/session "):
            self._handle_session_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/checkpoint delete" or text.startswith("/checkpoint delete "):
            await self._handle_delete_checkpoint_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/checkpoint rename" or text.startswith("/checkpoint rename "):
            self._handle_rename_checkpoint_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/checkpoint" or text.startswith("/checkpoint "):
            await self._handle_checkpoint_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/checkpoints clear" or text.startswith("/checkpoints clear "):
            await self._handle_clear_checkpoints_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/checkpoints" or text.startswith("/checkpoints "):
            self._handle_checkpoints_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/restore" or text.startswith("/restore "):
            await self._handle_restore_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/paper" or text.startswith("/paper "):
            await self._handle_paper_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/experiment" or text.startswith("/experiment "):
            await self._handle_experiment_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/experiments" or text.startswith("/experiments "):
            await self._handle_experiments_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/subagent" or text.startswith("/subagent "):
            self._handle_subagent_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/compact" or text.startswith("/compact "):
            await self._handle_compact_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if text == "/instructions" or text.startswith("/instructions "):
            await self._handle_instructions_command(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if _is_billing_query(text):
            await self._handle_bill_command(user_text=text)
            self.query_one("#user-input", TextArea).focus()
            return

        if self._pending_credential_scope is not None:
            if await self._handle_pending_credential(text):
                self.query_one("#user-input", TextArea).focus()
                return

        cds_credentials = _extract_cds_credentials(text)
        if cds_credentials is not None:
            cds_url, cds_key = cds_credentials
            await self._handle_local_cds_credentials(text, cds_url, cds_key)
            self.query_one("#user-input", TextArea).focus()
            return

        api_key = _extract_llm_api_key(text)
        if api_key and _mentions_vision_model(text):
            await self._handle_local_vision_key_setup(text, api_key)
            self.query_one("#user-input", TextArea).focus()
            return

        if _requests_primary_vision_reuse(text):
            await self._handle_local_vision_reuse(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if api_key:
            await self._handle_unscoped_api_key(text)
            self.query_one("#user-input", TextArea).focus()
            return

        if _requests_background_execution(text):
            self._enter_chat_mode()
            await self._mount_user_message(text)
            self._chat_log.append(f"你:\n{text}")
            await self._mount_background_handoff_message(
                text,
                title=_subagent_title_from_text(text),
                context_summary="用户明确要求该任务从一开始就在后台运行。",
            )
            self.query_one("#user-input", TextArea).focus()
            return

        if self._agent_worker is not None and self._agent_worker.is_running:
            if _should_queue_input_during_run(self._main_run_state):
                self._queued_message = text
                self._set_footer_status("消息已排队，等待回复完成...")
                return
            self._handoff_main_run_to_background()

        self._user_scrolled_up = False
        self._enter_chat_mode()
        await self._mount_user_message(text)
        self._chat_log.append(f"你:\n{text}")

        self._agent_msg = await self._mount_agent_message()
        self._status_msg = None
        debug_log("tui.chat_turn_started", text_length=len(text))
        self._maybe_scroll_to_end()

        if self.agent is None:
            error_text = t("app.agent_error", self.config.language).format(
                error=self.last_error
            )
            await self._agent_msg.update(f"**{error_text}**")
            self.query_one("#user-input", TextArea).focus()
            return

        self.sub_title = t("app.thinking", self.config.language)
        state = AgentRunState(
            agent=self.agent,
            agent_msg=self._agent_msg,
            user_text=text,
            base_messages=copy.deepcopy(self.agent.messages),
        )
        self._main_run_state = state
        self._agent_worker = self._run_agent(text, state)
        self._start_activity()

    async def _handle_local_llm_setup(self, text: str, setup: dict) -> None:
        self._enter_chat_mode()
        masked_text = _mask_secret_text(text)
        await self._mount_user_message(masked_text)
        self._chat_log.append(f"你:\n{masked_text}")

        agent_msg = await self._mount_agent_message()
        if not setup.get("api_key"):
            self._pending_llm_setup = None
            message = "主聊天模型和 API Key 不通过对话文本配置，请使用 `/provider` 打开配置界面。"
            await agent_msg.update(message)
            self._chat_log.append(f"Aero:\n{message}")
            return

        self._pending_llm_setup = None
        _apply_llm_setup(self.config, setup)
        self._refresh_agent_llm_config()
        self._refresh_model_info()
        message = _llm_setup_success_message(self.config)
        await agent_msg.update(message)
        self._chat_log.append(f"Aero:\n{message}")
        self._set_footer_status(message)

    async def _handle_local_cds_credentials(
        self, text: str, url: str, key: str
    ) -> None:
        """Store the official CDS two-line credential without sending it to the model."""
        self._enter_chat_mode()
        masked_text = _mask_cds_credentials(text)
        await self._mount_user_message(masked_text)
        self._chat_log.append(f"你:\n{masked_text}")

        self.config.credentials.cds.url = url
        self.config.credentials.cds.key = key
        self._pending_credential_scope = None
        save_cds_credentials(url, key)
        if self.persist_config:
            _save_config(self.config)
        self._record_local_credential_saved("cds")

        message = (
            "CDS API 凭证已在本地保存，未发送给模型。"
            "现在可以重新发送刚才的 ERA5 下载与绘图任务。"
        )
        agent_msg = await self._mount_agent_message()
        await agent_msg.update(message)
        self._chat_log.append(f"Aero:\n{message}")
        self._set_footer_status("CDS API 凭证已保存")

    def _record_local_credential_saved(self, scope: str) -> None:
        """Keep the model's conversation state in sync without exposing a secret.

        Credential text can be intercepted by the client and saved locally, so
        that turn never reaches AgentLoop.  Record only the successful state;
        otherwise a later "continue" can make the model ask for the same key.
        """
        if self.agent is None:
            return
        self.agent.messages.append(
            Message(
                role="assistant",
                content=(
                    f"System state: the {scope.upper()} credential is configured "
                    "locally and available for tools. Its value was not provided to "
                    "the model. Do not ask the user to enter it again unless a tool "
                    "reports that this specific credential is invalid or unavailable."
                ),
            )
        )

    async def _handle_pending_credential(self, text: str) -> bool:
        """Route a secret by the capability that explicitly requested it."""
        if self._pending_credential_scope != "cds":
            return False
        credentials = _extract_cds_credentials(text)
        if credentials is not None:
            await self._handle_local_cds_credentials(text, *credentials)
            return True
        key = _extract_llm_api_key(text)
        if not key:
            return False
        await self._handle_local_cds_credentials(
            text, self.config.credentials.cds.url, key
        )
        return True

    async def _handle_local_vision_key_setup(self, text: str, api_key: str) -> None:
        self._enter_chat_mode()
        masked_text = _mask_secret_text(text)
        await self._mount_user_message(masked_text)
        self._chat_log.append(f"你:\n{masked_text}")

        normalized_key = _normalize_pasted_api_key(api_key)
        self.config.vision.mode = "separate"
        self.config.vision.provider = "bailian"
        self.config.vision.api_key = normalized_key
        save_vision_api_key(
            normalized_key, self.config.vision.base_url, mode="separate"
        )
        if self.persist_config:
            _save_config(self.config)

        message = (
            "百炼 API Key 已在本地更新。主聊天模型没有切换；"
            "联网搜索会自动使用该凭证。"
        )
        agent_msg = await self._mount_agent_message()
        await agent_msg.update(message)
        self._chat_log.append(f"Aero:\n{message}")
        self._set_footer_status("视觉模型凭证已更新")

    async def _handle_local_vision_reuse(self, text: str) -> None:
        """Enable true reuse from the active primary profile without model guessing."""
        self._enter_chat_mode()
        await self._mount_user_message(text)
        self._chat_log.append(f"你:\n{text}")

        if self.config.llm.supports_vision and self.config.llm.active_api_key():
            self._reuse_primary_vision_model()
            vision = resolved_vision_config(self.config)
            assert vision is not None
            message = (
                "视觉能力已设置为复用当前主模型："
                f"{_display_vision_provider_name(vision.provider)}/"
                f"{_display_vision_model_name(vision.model)}。"
                "图片将使用该模型分析，不会切换到百炼或其他服务商。"
            )
        else:
            message = (
                "当前主模型不支持视觉能力，或其 API Key 尚未配置；"
                "无法复用。请在 /vision 中选择独立视觉模型。"
            )
        agent_msg = await self._mount_agent_message()
        await agent_msg.update(message)
        self._chat_log.append(f"Aero:\n{message}")

    async def _handle_unscoped_api_key(self, text: str) -> None:
        """Keep ambiguous secrets out of model prompts and conversation logs."""
        self._enter_chat_mode()
        masked_text = _mask_secret_text(text)
        await self._mount_user_message(masked_text)
        self._chat_log.append(f"你:\n{masked_text}")

        message = (
            "检测到 API Key，已隐藏且未发送给模型，本次也没有保存。"
            "主聊天模型请使用 `/provider` 配置；视觉模型请使用 `/vision`；"
            "网页搜索请使用 `/websearch provider` 选择服务后，再执行 `/websearch on`。"
        )
        agent_msg = await self._mount_agent_message()
        await agent_msg.update(message)
        self._chat_log.append(f"Aero:\n{message}")

    async def _handle_local_llm_clear(self, text: str, *, reset_provider: bool = False) -> None:
        self._enter_chat_mode()
        await self._mount_user_message(text)
        self._chat_log.append(f"你:\n{text}")

        _clear_llm_setup(self.config, reset_provider=reset_provider)
        self._refresh_agent_llm_config()
        self._refresh_model_info()
        message = _llm_clear_success_message(self.config, reset_provider=reset_provider)
        agent_msg = await self._mount_agent_message()
        await agent_msg.update(message)
        self._chat_log.append(f"Aero:\n{message}")
        self._set_footer_status(message)

    def _refresh_agent_llm_config(self) -> None:
        if self.agent is None:
            self._init_agent()
            return
        self.agent.config.llm.provider = self.config.llm.provider
        self.agent.config.llm.model = self.config.llm.model
        self.agent.config.llm.reasoning_effort = self.config.llm.reasoning_effort
        self.agent.config.llm.providers = self.config.llm.providers
        self.agent.config.llm.base_url = self.config.llm.base_url
        self.agent.llm.config.provider = self.config.llm.provider
        self.agent.llm.config.model = self.config.llm.model
        self.agent.llm.config.reasoning_effort = self.config.llm.reasoning_effort
        self.agent.llm.config.api_key = self.config.llm.active_api_key()
        self.agent.llm.config.base_url = self.config.llm.base_url

    async def _mount_user_message(self, text: str) -> None:
        self._last_user_text = text
        chat = self.query_one("#chat-area", VerticalScroll)
        row = Horizontal(classes="message-row user-message")
        await chat.mount(row)
        await row.mount(
            Static(
                t("label.user", self.config.language),
                classes="message-label user-label",
            )
        )
        await row.mount(Static(escape(text), classes="message-body user-content"))

    async def _mount_agent_message(self) -> Markdown:
        chat = self.query_one("#chat-area", VerticalScroll)
        row = Horizontal(classes="message-row agent-message")
        await chat.mount(row)
        await row.mount(
            Static(
                t("label.agent", self.config.language),
                classes="message-label agent-label",
            )
        )
        stack = Vertical(classes="message-body agent-stack")
        await row.mount(stack)
        body = ChatMarkdown("", classes="agent-content")
        await stack.mount(body)
        return body

    def _resolve_inline_image_paths(self, text: str) -> list[Path]:
        """Resolve image references in a chat message to existing local files."""
        resolved_paths: list[Path] = []
        seen: set[Path] = set()
        experiment = self._get_experiment_mgr().active()
        workspace = (
            self._get_experiment_mgr().workspace_path(experiment)
            if experiment is not None
            else self._project_dir
        )
        search_dirs = [
            workspace,
            workspace / "data",
            self._project_dir,
            self._project_dir / "data",
        ]
        for raw_path in extract_image_paths(text)[:4]:
            resolved = None
            for base in search_dirs:
                candidate = base / raw_path
                if candidate.exists() and candidate.is_file():
                    resolved = candidate.resolve()
                    break
            if resolved is None:
                p = Path(raw_path).expanduser()
                if not p.is_absolute():
                    p = (workspace / p).resolve()
                if p.exists() and p.is_file():
                    resolved = p.resolve()
            if resolved is None or resolved in seen:
                continue
            seen.add(resolved)
            resolved_paths.append(resolved)
        return resolved_paths

    def _register_image_attachment(self, path: Path) -> int:
        """Record an image for /preview and return its 1-based preview index."""
        try:
            index = self._image_attachments.index(path)
        except ValueError:
            self._image_attachments.append(path)
            index = len(self._image_attachments) - 1
        return index + 1

    async def _render_inline_images(self, text: str) -> None:
        """Replace ![](path) markers in agent message with inline images."""
        paths = self._resolve_inline_image_paths(text)
        if not paths:
            return

        clean_text = strip_image_markdown(text)
        if self._agent_msg is not None and clean_text != text:
            await self._agent_msg.update(clean_text)

        stack = getattr(self._agent_msg, "parent", None)
        if stack is None:
            return
        for path in paths:
            try:
                index = self._register_image_attachment(path)
                await stack.mount(InlineImageAttachment(path, index))
            except Exception:
                debug_log("tui.inline_image_failed", path=str(path))
        self._maybe_scroll_to_end()

    async def _safe_update_agent_message(self, message: str) -> None:
        if self._agent_msg is None:
            return
        try:
            await self._agent_msg.update(message)
        except Exception as e:
            debug_log(
                "tui.agent_markdown_update_failed",
                error=repr(e),
                traceback=traceback.format_exc(),
                message_length=len(message),
            )
            fallback = "\n".join(f"    {line}" for line in message.splitlines()) or "    "
            try:
                await self._agent_msg.update(fallback)
            except Exception as fallback_error:
                debug_log(
                    "tui.agent_markdown_fallback_failed",
                    error=repr(fallback_error),
                    traceback=traceback.format_exc(),
                    message_length=len(message),
                )
                await self._agent_msg.update("")

    def _cancel_agent_run(self) -> None:
        debug_log(
            "tui.cancel_requested",
            has_agent=self.agent is not None,
            worker_running=self._agent_worker is not None and self._agent_worker.is_running,
        )
        if self.agent is not None:
            self.agent.cancel()
        if self._agent_worker is not None and self._agent_worker.is_running:
            self._agent_worker.cancel()
            self._stop_activity()
            self._append_status("已中断当前对话")
            self._collapse_status(done=True)
            self.sub_title = "已中断 " + self._ready_subtitle()
            self.query_one("#user-input", TextArea).focus()
        else:
            self.notify("当前没有正在运行的对话", timeout=2)

    def _handle_set_command(self, text: str) -> None:
        lang = self.config.language
        parts = text.split()
        if len(parts) < 3:
            self._set_footer_status(_strip_markup(t("error.usage_set", lang)))
            return
        if parts[1] == "model":
            self._set_model(parts[2])
            return
        if parts[1] in ("variants", "effort", "reasoning_effort"):
            self._set_reasoning_effort(parts[2])
            return
        if len(parts) != 3 or parts[1] != "max_tool_rounds":
            self._set_footer_status(_strip_markup(t("error.usage_set_tool_rounds", lang)))
            return
        try:
            value = int(parts[2])
        except ValueError:
            self._set_footer_status(_strip_markup(t("error.max_tool_rounds_int", lang)))
            return
        if value < 1:
            self._set_footer_status(_strip_markup(t("error.max_tool_rounds_range", lang)))
            return
        if self.agent is None:
            self._set_footer_status(_strip_markup(t("error.agent_init", lang).format(error=self.last_error)))
            return
        self.agent.max_tool_rounds = value
        self.config.max_tool_rounds = value
        from aero.toolbox.builtin_tools import set_max_tool_rounds
        set_max_tool_rounds(value)
        if getattr(self, "persist_config", False):
            _save_config(self.config)
        self._set_footer_status(_strip_markup(t("info.tool_rounds_set", lang).format(value=value)))

    def _handle_language_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            self.push_screen(
                SelectScreen(
                    t("app.language_select_title", self.config.language),
                    language_options(),
                    self.config.language,
                    self.config.language,
                ),
                callback=self._apply_selected_language,
            )
            return
        value = parts[1].strip()
        if not is_supported_language(value):
            chat = self.query_one("#chat-area", VerticalScroll)
            chat.mount(Static(t("app.language_bad", self.config.language)))
            return
        self._set_language(value)

    def _apply_selected_language(self, selected: str | None) -> None:
        if selected is not None:
            self._set_language(selected)
        self.query_one("#user-input", TextArea).focus()

    def _set_language(self, value: str) -> None:
        label = language_label(value)
        self.config.language = value
        if self.agent is not None:
            self.agent.reset_system_prompt(value)
        if self.persist_config:
            _save_config(self.config)
        self._refresh_ui_language()
        self._set_footer_status(t("app.language_switched", value).format(label=label))

    def _refresh_ui_language(self) -> None:
        lang = self.config.language

        # Subtitle
        self.sub_title = t("app.ready", lang)

        # Update message labels in chat
        try:
            chat = self.query_one("#chat-area", VerticalScroll)
            for label in chat.query(".message-label.user-label"):
                label.update(t("label.user", lang))
            for label in chat.query(".message-label.agent-label"):
                label.update(t("label.agent", lang))
        except Exception:
            pass

        # Command descriptions for autocomplete
        self._refresh_commands()

        # Refresh model info status line
        self._refresh_model_info()

    def _refresh_commands(self) -> None:
        self._commands_list = [
            ("/bill", t("cmd.bill", self.config.language)),
            ("/clear", t("cmd.clear", self.config.language)),
            ("/copy", t("cmd.copy", self.config.language)),
            ("/help", t("cmd.help", self.config.language)),
            ("/language", t("cmd.language", self.config.language)),
            ("/model", t("cmd.model", self.config.language)),
            ("/new", t("cmd.new", self.config.language)),
            ("/preview", t("cmd.preview", self.config.language)),
            ("/provider", t("cmd.provider", self.config.language)),
            ("/quit", t("cmd.quit", self.config.language)),
            ("/revoke", t("cmd.revoke", self.config.language)),
            ("/set", t("cmd.set", self.config.language)),
            ("/theme", t("cmd.theme", self.config.language)),
            ("/variants", t("cmd.variants", self.config.language)),
            ("/vision", t("cmd.vision", self.config.language)),
            ("/websearch", "开启/关闭网页搜索"),
            ("/mode", t("cmd.mode", self.config.language)),
            ("/session", t("cmd.session", self.config.language)),
            ("/checkpoint", t("cmd.checkpoint", self.config.language)),
            ("/checkpoints", t("cmd.checkpoints", self.config.language)),
            ("/restore", t("cmd.restore", self.config.language)),
            ("/paper", t("cmd.paper", self.config.language)),
            ("/experiment", t("cmd.experiment", self.config.language)),
            ("/experiments", t("cmd.experiments", self.config.language)),
            ("/compact", t("cmd.compact", self.config.language)),
            ("/instructions", t("cmd.instructions", self.config.language)),
            ("/subagent", t("cmd.subagent", self.config.language)),
        ]
        if OFFICIAL_ACCOUNT_UI_ENABLED:
            self._commands_list.extend(
                [
                    ("/account", "查看官方账户与额度"),
                    ("/login", "登录 Aerolytica 官方账户"),
                    ("/logout", "退出 Aerolytica 官方账户"),
                ]
            )
            self._commands_list.sort(key=lambda command: command[0])
        self._secondary_commands_list = [
            ("/websearch on", "开启网页搜索"),
            ("/websearch off", "关闭网页搜索"),
            ("/websearch status", "查看网页搜索状态"),
            ("/websearch provider", "切换网页搜索服务"),
            ("/set max_tool_rounds ", "设置最大工具调用轮次"),
            ("/set model ", "切换模型  如 flash 或 pro"),
            ("/set variants ", "设置推理强度  low/medium/high/max/auto"),
            ("/session rename ", "修改当前会话标题"),
            ("/checkpoint ", "创建带名称的检查点"),
            ("/checkpoint delete ", "永久删除指定检查点"),
            ("/checkpoint rename ", "修改指定检查点的名称"),
            ("/checkpoints all", "包含恢复保护记录的完整列表"),
            ("/checkpoints clear", "清理全部检查点"),
            ("/restore ", "预览差异并恢复检查点"),
            ("/paper init", "创建并绑定 paper/main.md"),
            ("/paper status", "查看论文正文是否有未保存变化"),
            ("/paper save ", "保存当前论文版本"),
            ("/paper versions", "查看论文版本历史"),
            ("/paper versions all", "包含恢复保护版本的完整历史"),
            ("/paper diff ", "比较当前正文与已保存版本"),
            ("/paper restore ", "预览并恢复论文版本"),
            ("/paper export latex", "导出 LaTeX 文档"),
            ("/paper export word", "导出并嵌入图片的 Word 文档"),
            ("/paper export pdf", "导出并嵌入图片的 PDF 文档"),
            ("/experiment new ", "创建独立实验工作区"),
            ("/experiment delete ", "永久删除指定实验"),
            ("/experiment finish", "总结并完成当前实验"),
            ("/experiments switch ", "切换到指定实验"),
            ("/experiments clear", "清理全部实验"),
            ("/instructions clear", "清空当前项目指令"),
            ("/subagent list", "查看后台任务"),
            ("/subagent cancel ", "取消后台任务"),
            ("/subagent allow ", "允许暂停中的后台任务继续"),
            ("/subagent deny ", "拒绝暂停中的后台任务"),
        ]

    def _handle_model_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        lang = self.config.language
        if len(parts) == 1:
            self.push_screen(
                SelectScreen(
                    "Select model",
                    _model_options(self.config.llm.provider),
                    self.config.llm.model,
                    lang,
                ),
                callback=self._apply_selected_model,
            )
            return
        self._set_model(parts[1].strip())

    def _handle_preview_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        lang = self.config.language

        if arg in {"list", "ls", "列表"}:
            if not self._image_attachments:
                message = _strip_markup(t("preview.none", lang))
                self._set_footer_status(message)
                self.notify(message, severity="warning", timeout=2)
                return
            items = []
            for idx, path in enumerate(self._image_attachments, start=1):
                width, height, size_text = image_metadata(path)
                dimensions = f"{width}x{height}" if width and height else "unknown"
                items.append(f"{idx}. {path.name} ({dimensions}, {size_text})")
            title = _strip_markup(t("preview.list_title", lang))
            message = f"{title}: " + "  ".join(items)
            self._set_footer_status(message, timeout=8.0)
            self.notify(message, timeout=4)
            return

        target = self._resolve_preview_target(arg)
        if target is None:
            message = _strip_markup(t("preview.not_found", lang))
            self._set_footer_status(message)
            self.notify(message, severity="warning", timeout=3)
            return

        try:
            self._open_image_file(target)
        except Exception as e:
            message = _strip_markup(t("preview.open_failed", lang, error=str(e)))
            self._set_footer_status(message)
            self.notify(message, severity="error", timeout=3)
            return

        message = _strip_markup(t("preview.opened", lang, path=str(target)))
        self._set_footer_status(message)
        self.notify(message, timeout=2)

    def _resolve_preview_target(self, arg: str) -> Path | None:
        if not arg:
            return self._image_attachments[-1] if self._image_attachments else None

        if arg.isdigit():
            index = int(arg) - 1
            if 0 <= index < len(self._image_attachments):
                return self._image_attachments[index]
            return None

        resolved = resolve_image_path(arg, Path("."))
        if resolved is not None:
            return resolved.resolve()

        candidate = Path(arg).expanduser()
        if not candidate.is_absolute():
            candidate = Path(".") / candidate
        candidate = candidate.resolve()
        if candidate.exists() and candidate.is_file():
            return candidate
        return None

    def _open_image_file(self, path: Path) -> None:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=True)
            return
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
            return
        subprocess.run(["xdg-open", str(path)], check=True)

    def _handle_provider_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        lang = self.config.language
        if len(parts) == 1:
            self.push_screen(
                SelectScreen(
                    "Select provider",
                    provider_options(),
                    self.config.llm.provider,
                    lang,
                ),
                callback=self._apply_selected_provider,
            )
            return
        self._set_provider(parts[1].strip())

    async def _handle_official_login(self) -> None:
        from aero.core.official_account import (
            OfficialAccountError,
            OfficialAccountSession,
            relay_llm_url,
        )

        credentials = await self.push_screen_wait(OfficialLoginScreen())
        if credentials is None:
            return
        session = OfficialAccountSession()
        self._set_footer_status("正在登录 Aerolytica 官方账户…")
        try:
            account = await session.login(credentials["email"], credentials["password"])
        except OfficialAccountError as exc:
            self.notify(str(exc), severity="error", timeout=5)
            self._set_footer_status(str(exc))
            return
        finally:
            await session.close()

        self.config.llm.provider = "official"
        self.config.llm.model = "auto"
        self.config.llm.base_url = relay_llm_url()
        self.config.llm.supports_vision = False
        self.config.llm.apply_active_provider_defaults()
        save_llm_profile("official", "", "auto", self.config.llm.base_url)
        if self.persist_config:
            _save_config(self.config)
        self._init_agent()
        await self._refresh_official_credits()
        self._refresh_model_info()
        self._set_footer_status(
            f"已登录 {account.email}，当前使用 Aerolytica 官方模型。"
        )

    async def _handle_official_logout(self) -> None:
        from aero.core.official_account import OfficialAccountSession

        session = OfficialAccountSession()
        if not session.data.is_logged_in:
            await session.close()
            self._set_footer_status("当前未登录 Aerolytica 官方账户。")
            return
        try:
            revoked = await session.logout()
        except Exception:
            revoked = False
        finally:
            await session.close()
        message = (
            "已退出 Aerolytica 官方账户。"
            if revoked
            else "已清除本地登录；服务端会话撤销状态未确认。"
        )
        if self.config.llm.provider == "official":
            message += (
                " 当前仍选择官方模型，请重新登录，"
                "或使用 `/provider` 切换模型服务。"
            )
        self._official_available_credits = None
        self._refresh_model_info()
        self._set_footer_status(message)

    async def _handle_official_account(self) -> None:
        from aero.core.official_account import OfficialAccountError, OfficialAccountSession

        session = OfficialAccountSession()
        if not session.data.is_logged_in:
            await session.close()
            self._set_footer_status("请先使用 `/login` 登录 Aerolytica 官方账户。")
            return
        try:
            user, credits = await asyncio.gather(session.me(), session.credits())
        except OfficialAccountError as exc:
            self.notify(str(exc), severity="error", timeout=5)
            self._set_footer_status(str(exc))
            return
        finally:
            await session.close()
        subscription = credits.get("subscription")
        subscription = subscription if isinstance(subscription, dict) else {}
        email = str(user.get("email") or session.data.email or "未知")
        available = credits.get("available_credits", "未知")
        self._official_available_credits = _format_credit_balance(available)
        reserved = credits.get("reserved_credits", "未知")
        plan = subscription.get("plan_name") or subscription.get("plan_id") or "无"
        period_end = subscription.get("period_end") or "—"
        message = (
            "### Aerolytica 官方账户\n\n"
            f"- 邮箱：`{email}`\n"
            f"- 套餐：{plan}\n"
            f"- 可用额度：{available} credits\n"
            f"- 冻结额度：{reserved} credits\n"
            f"- 当前周期结束：{period_end}"
        )
        self.query_one("#chat-area", VerticalScroll).mount(Markdown(message))
        self._refresh_model_info()

    async def _refresh_official_credits(self) -> None:
        """Refresh the official balance without making chat depend on billing."""
        from aero.core.official_account import OfficialAccountSession

        if self.config.llm.provider != "official":
            return
        session = OfficialAccountSession()
        if not session.data.is_logged_in:
            self._official_available_credits = None
            await session.close()
            self._refresh_model_info()
            return
        try:
            credits = await session.credits()
        except Exception as exc:
            debug_log("official_account.credits_refresh_failed", error=repr(exc))
            return
        finally:
            await session.close()
        self._official_available_credits = _format_credit_balance(
            credits.get("available_credits")
        )
        self._refresh_model_info()

    def _handle_variants_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        lang = self.config.language
        if len(parts) == 1:
            self.push_screen(
                SelectScreen(
                    "Select variant",
                    _variant_options(),
                    self.config.llm.reasoning_effort,
                    lang,
                ),
                callback=self._apply_selected_variant,
            )
            return
        self._set_reasoning_effort(parts[1].strip())

    def _handle_theme_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        lang = self.config.language
        if len(parts) == 1:
            self._theme_before_select = self.theme
            self.push_screen(
                SelectScreen(
                    t("app.theme_select_title", lang),
                    _theme_options(self.available_themes),
                    self.theme,
                    lang,
                    on_highlight=self._preview_theme,
                ),
                callback=self._apply_selected_theme,
            )
            return
        self._set_theme(parts[1].strip())

    def _apply_selected_model(self, selected: str | None) -> None:
        if selected is not None:
            self._set_model(selected)
        self.query_one("#user-input", TextArea).focus()

    def _apply_selected_provider(self, selected: str | None) -> None:
        if selected is not None:
            self._set_provider(selected)
        self.query_one("#user-input", TextArea).focus()

    def _apply_selected_variant(self, selected: str | None) -> None:
        if selected is not None:
            self._set_reasoning_effort(selected)
        self.query_one("#user-input", TextArea).focus()

    def _apply_selected_theme(self, selected: str | None) -> None:
        if selected is not None:
            self._set_theme(selected)
        elif self._theme_before_select is not None:
            self.theme = self._theme_before_select
        self._theme_before_select = None
        self.query_one("#user-input", TextArea).focus()

    def _handle_vision_command(self, text: str) -> None:
        from aero.data.vision_models import is_valid_vision_model, vision_model_options

        parts = text.split(maxsplit=1)
        lang = self.config.language
        if len(parts) == 1:
            options = vision_model_options()
            current = self.config.vision.model
            primary_vision = resolved_vision_config(self.config)
            if self.config.vision.mode == "reuse_primary" and primary_vision is not None:
                primary_label = (
                    f"复用主模型：{_display_vision_model_name(primary_vision.model)}"
                    f"（{_display_vision_provider_name(primary_vision.provider)}）"
                )
                options = [(_REUSE_PRIMARY_VISION_OPTION, primary_label), *options]
                current = _REUSE_PRIMARY_VISION_OPTION
            self.push_screen(
                SelectScreen(
                    t("app.vision_select_title", lang),
                    options,
                    current,
                    lang,
                ),
                callback=self._apply_selected_vision_model,
            )
            return
        value = parts[1].strip()
        if not is_valid_vision_model(value):
            chat = self.query_one("#chat-area", VerticalScroll)
            chat.mount(Static(t("app.vision_bad", lang)))
            return
        self._set_vision_model(value)

    def _apply_selected_vision_model(self, selected: str | None) -> None:
        if selected == _REUSE_PRIMARY_VISION_OPTION:
            self._reuse_primary_vision_model()
        elif selected is not None:
            self._set_vision_model(selected)
        self.query_one("#user-input", TextArea).focus()

    def _reuse_primary_vision_model(self, *, persist: bool = True) -> None:
        """Snapshot the selected multimodal primary model for visual use.

        The primary chat model may later be switched to a text-only provider.
        Keep the selected provider/model here and resolve its key through the
        stored provider profile instead of following that later switch.
        """
        self.config.vision.mode = "reuse_primary"
        self.config.vision.provider = self.config.llm.provider
        self.config.vision.model = self.config.llm.model
        self.config.vision.base_url = self.config.llm.base_url
        # The key remains in the provider profile / user secrets; do not copy
        # it into the visual profile.
        self.config.vision.api_key = ""
        save_vision_profile(
            "reuse_primary",
            provider=self.config.vision.provider,
            model=self.config.vision.model,
            base_url=self.config.vision.base_url,
        )
        if persist and self.persist_config:
            _save_config(self.config)
        self._refresh_model_info()
        self._set_footer_status("视觉能力已保存为复用所选主模型")

    def _set_vision_model(self, model: str) -> None:
        lang = self.config.language
        self.config.vision.mode = "separate"
        self.config.vision.provider = "bailian"
        self.config.vision.model = model
        bailian_profile = self.config.llm.provider_config("bailian")
        self.config.vision.base_url = (
            bailian_profile.base_url or get_provider_preset("bailian").base_url
        )
        save_vision_profile(
            "separate",
            provider=self.config.vision.provider,
            model=self.config.vision.model,
            base_url=self.config.vision.base_url,
        )
        if self.persist_config:
            _save_config(self.config)
        self._refresh_model_info()
        self._set_footer_status(
            t("app.vision_switched", lang).format(model=model)
        )

    async def _handle_websearch_command(self, text: str) -> None:
        """Toggle the explicitly configured external search service."""
        if self.config.llm.provider == "official":
            self._set_footer_status(
                "Aerolytica 官方渠道使用 Relay 托管的 MCP 联网搜索，"
                "无需配置客户端搜索 API Key。"
            )
            return
        from aero.data.web_search import (
            check_bailian_web,
            search_zhipu_web,
        )

        parts = text.split(maxsplit=1)
        value = parts[1].strip().lower() if len(parts) > 1 else ""
        if not value:
            current = "on" if self.config.web_search.enabled else "off"
            self.push_screen(
                SelectScreen(
                    "网页搜索",
                    [
                        ("on", "开启网页搜索"),
                        ("off", "关闭网页搜索"),
                        ("status", "查看当前状态"),
                    ],
                    current,
                    self.config.language,
                ),
                callback=self._apply_selected_websearch,
            )
            return
        if value == "provider" or value.startswith("provider "):
            selected = value.split(maxsplit=1)[1] if " " in value else ""
            if selected not in {"bailian", "zhipu"}:
                self.push_screen(
                    SelectScreen(
                        "选择网页搜索服务",
                        [("bailian", "阿里云百炼 WebSearch MCP"), ("zhipu", "智谱 AI 搜索")],
                        self.config.web_search.provider or "bailian",
                        self.config.language,
                    ),
                    callback=self._apply_websearch_provider,
                )
            else:
                self._apply_websearch_provider(selected)
            return
        if value not in {"on", "off"}:
            if value == "status":
                self._show_websearch_status()
            else:
                self._set_footer_status("用法：/websearch on、/websearch off 或 /websearch status")
            return
        if value == "off":
            self.config.web_search.enabled = False
            save_web_search_state(enabled=False)
            if self.agent is not None:
                self.agent.config.web_search.enabled = False
            self._set_footer_status("网页搜索已关闭。")
            return

        provider = (self.config.web_search.provider or "bailian").strip().lower()
        # Search credentials are capability-scoped. Never silently reuse a
        # primary/vision key; ask for explicit authorization below.
        api_key = self.config.web_search.api_key.strip()
        if not api_key:
            candidate = self._websearch_reuse_candidate(provider)
            if candidate:
                choice = await self.push_screen_wait(
                    ConfirmScreen(
                        (
                            f"检测到已配置的{provider} API Key。\n\n"
                            "是否明确授权将它复用为网页搜索服务凭证？\n"
                            "授权后密钥仅保存在本地，不会写入对话。"
                        ),
                        self.config.language,
                        allow_label="授权复用",
                        deny_label="改为输入新 Key",
                    )
                )
                if choice == "allow":
                    api_key = candidate
                    self.config.web_search.api_key = api_key
                    save_web_search_api_key(
                        api_key,
                        self.config.web_search.base_url,
                        provider=provider,
                        model=self.config.web_search.model,
                    )
            if not api_key:
                await self._prompt_websearch_key(provider)
                api_key = self.config.web_search.api_key.strip()
            if not api_key:
                self._set_footer_status("未配置网页搜索 API Key，网页搜索保持关闭。")
                return

        if (
            self.config.web_search.mcp_verified
            and self.config.web_search.mcp_verified_provider == provider
        ):
            self.config.web_search.enabled = True
            save_web_search_state(enabled=True)
            if self.agent is not None:
                self.agent.config.web_search.enabled = True
            self._set_footer_status("网页搜索已开启（已复用之前的连通性测试结果）。")
            return
        try:
            if provider == "bailian":
                await check_bailian_web(api_key)
            elif provider == "zhipu":
                await search_zhipu_web(api_key, "联网搜索连通性测试", limit=1)
            else:
                raise RuntimeError(f"不支持的搜索供应商：{provider}")
        except Exception as exc:
            self.config.web_search.enabled = False
            save_web_search_state(enabled=False, mcp_verified=False)
            self._set_footer_status(f"网页搜索连通性测试失败，未开启：{str(exc)[:180]}")
            return
        self.config.web_search.enabled = True
        self.config.web_search.mcp_verified = True
        self.config.web_search.mcp_verified_provider = provider
        self.config.web_search.mcp_verified_at = time.time()
        save_web_search_state(
            enabled=True,
            mcp_verified=True,
            mcp_verified_provider=provider,
            mcp_verified_at=self.config.web_search.mcp_verified_at,
        )
        if self.agent is not None:
            self.agent.config.web_search.enabled = True
        self._set_footer_status("网页搜索已开启，连通性测试通过。")

    def _websearch_reuse_candidate(self, provider: str) -> str:
        """Return a same-provider key only as a candidate for explicit consent."""
        if provider == (self.config.llm.provider or "").strip().lower():
            key = self.config.llm.active_api_key().strip()
            if key:
                return key
        if provider == (self.config.vision.provider or "").strip().lower():
            key = self.config.vision.api_key.strip()
            if key:
                return key
        return ""

    def _apply_websearch_provider(self, selected: str | None) -> None:
        if selected not in {"bailian", "zhipu"}:
            self.query_one("#user-input", TextArea).focus()
            return
        self.config.web_search.provider = selected
        self.config.web_search.api_key = ""
        self.config.web_search.enabled = False
        self.config.web_search.mcp_verified = False
        self.config.web_search.mcp_verified_provider = ""
        self.config.web_search.mcp_verified_at = 0.0
        save_web_search_state(
            enabled=False,
            mcp_verified=False,
            mcp_verified_provider="",
            mcp_verified_at=0.0,
        )
        self._set_footer_status(
            f"已选择网页搜索服务：{'阿里云百炼' if selected == 'bailian' else '智谱 AI'}；"
            "尚未配置搜索 API Key，连通性测试未执行，网页搜索未启用。"
        )
        self.query_one("#user-input", TextArea).focus()

    async def _prompt_websearch_key(self, provider: str) -> None:
        if provider == "zhipu":
            title = "输入智谱 AI 网页搜索 API Key"
            guide = (
                "请访问 https://open.bigmodel.cn/apikey/platform 创建并复制 API Key。\n"
                "智谱搜索无需开通百炼 MCP；请确认智谱账户余额和搜索调用额度可用。\n"
                "密钥只在本地保存，不会写入对话，也不会发送给模型。"
            )
        else:
            title = "输入阿里云百炼网页搜索 API Key"
            guide = (
                "请访问 https://bailian.console.aliyun.com/cn-beijing/"
                "?apiKey=1&tab=globalset#/efm/api_key 创建或复制 DashScope API Key。\n"
                "然后进入百炼 MCP 广场，搜索“WebSearch”或“联网搜索”，"
                "点击“立即开通 → 确认开通”。\n"
                "API Key 与 MCP 开通两项缺一不可；请同时确认账户余额和调用额度可用。\n"
                "计费：全部用户前 2000 次调用免费，免费额度用尽后按 29 元/千次计费；"
                "价格可能调整，以阿里云官方页面为准。\n"
                "密钥只在本地保存，不会写入对话，也不会发送给模型。"
            )
        entered = await self.push_screen_wait(
            SecretInputScreen(
                SecretInputRequest(
                    scope="web_search",
                    title=title,
                    instructions=guide,
                    multiline=False,
                )
            )
        )
        if entered:
            self.config.web_search.api_key = str(entered).strip()
            save_web_search_api_key(
                self.config.web_search.api_key,
                self.config.web_search.base_url,
                provider=provider,
                model=self.config.web_search.model,
            )

    def _apply_selected_websearch(self, selected: str | None) -> None:
        if selected:
            self.run_worker(
                self._handle_websearch_command(f"/websearch {selected}"),
                exclusive=True,
            )
        self.query_one("#user-input", TextArea).focus()

    def _show_websearch_status(self) -> None:
        """Show local state without issuing a network request."""
        if not self.config.web_search.enabled:
            self._show_checkpoint_message(
                "### 网页搜索状态\n\n当前：**关闭**\n\n使用 `/websearch on` 开启。",
                force_scroll=True,
            )
            return
        provider = self.config.web_search.provider or "bailian"
        verified = (
            self.config.web_search.mcp_verified
            and self.config.web_search.mcp_verified_provider == provider
        )
        detail = (
            f"当前：**已开启**\n\n外部搜索服务：`{provider}`\n"
            f"API Key：{'已配置' if self.config.web_search.api_key else '未单独配置（可能复用视觉 Key）'}\n"
            f"连通性测试：{'已通过' if verified else '未通过或未测试'}"
        )
        self._show_checkpoint_message("### 网页搜索状态\n\n" + detail, force_scroll=True)

    async def _handle_bill_command(self, *, user_text: str | None = None) -> None:
        """Render the shared session cost ledger without making an LLM call."""
        if user_text:
            await self._mount_user_message(user_text)
            self._chat_log.append(f"{t('label.user', self.config.language)}:\n{user_text}")
        tracker = self.agent.tracker if self.agent is not None else TokenTracker()
        message = _billing_markdown(tracker, self.config.language)
        agent_msg = await self._mount_agent_message()
        if isinstance(agent_msg, ChatMarkdown):
            agent_msg.force_scroll_on_ready = True
        await agent_msg.update(message)
        self._last_reply_text = message
        self._chat_log.append(f"Aero:\n{message}")
        self._force_scroll_chat_to_end()
        self.call_after_refresh(self._force_scroll_chat_to_end)
        self.set_timer(0.05, self._force_scroll_chat_to_end)

    def _handle_mode_command(self, text: str) -> None:
        from aero.data.modes import MODE_LABELS, MODE_OPTIONS

        parts = text.split(maxsplit=1)
        lang = self.config.language
        if len(parts) == 1:
            self.push_screen(
                SelectScreen(
                    t("app.mode_select_title", lang),
                    [(v, label) for v, label in MODE_OPTIONS],
                    self.config.mode,
                    lang,
                ),
                callback=self._apply_selected_mode,
            )
            return
        value = parts[1].strip()
        if value not in MODE_LABELS:
            chat = self.query_one("#chat-area", VerticalScroll)
            chat.mount(Static(t("app.mode_bad", lang)))
            return
        self._set_mode(value)

    def _apply_selected_mode(self, selected: str | None) -> None:
        if selected is not None:
            self._set_mode(selected)
        self.query_one("#user-input", TextArea).focus()

    def _set_mode(self, mode: str) -> None:
        lang = self.config.language
        self.config.mode = mode
        if self.persist_config:
            _save_config(self.config)
        if self.agent is not None:
            self.agent.config.mode = mode
            self.agent.reset_system_prompt(lang)
        self._refresh_mode_ui()
        from aero.data.modes import MODE_LABELS
        self._set_footer_status(
            t("app.mode_switched", lang).format(mode=MODE_LABELS.get(mode, mode))
        )

    def action_cycle_mode(self) -> None:
        from aero.data.modes import MODE_ORDER
        current = self.config.mode
        try:
            idx = MODE_ORDER.index(current)
        except ValueError:
            idx = 0
        next_mode = MODE_ORDER[(idx + 1) % len(MODE_ORDER)]
        self._set_mode(next_mode)

    def _refresh_mode_ui(self) -> None:
        from aero.data.modes import MODE_LABELS

        input_box = self.query_one("#input-box")
        for cls in ("mode-plan", "mode-execute", "mode-qa"):
            input_box.remove_class(cls)
        mode = self.config.mode
        if mode in ("plan", "execute", "qa"):
            input_box.add_class(f"mode-{mode}")

        label = self.query_one("#mode-label", Static)
        label.update(f"── {MODE_LABELS.get(mode, mode)} ──")

    def _get_session_mgr(self):
        if self._session_mgr is None:
            from aero.agent.session import SessionManager
            self._session_mgr = SessionManager()
        return self._session_mgr

    def _get_checkpoint_mgr(self):
        active_experiment = self._get_experiment_mgr().active()
        if active_experiment is not None:
            from aero.checkpoints import CheckpointManager

            workspace = self._get_experiment_mgr().workspace_path(active_experiment)
            return CheckpointManager(
                workspace,
                experiment_id=active_experiment["id"],
            )
        return self._get_main_checkpoint_mgr()

    def _get_main_checkpoint_mgr(self):
        if self._checkpoint_mgr is None:
            from aero.checkpoints import CheckpointManager

            self._checkpoint_mgr = CheckpointManager(self._project_dir)
        return self._checkpoint_mgr

    def _get_experiment_mgr(self):
        if self._experiment_mgr is None:
            from aero.experiments import ExperimentManager

            self._experiment_mgr = ExperimentManager(self._project_dir)
        return self._experiment_mgr

    def _get_experiment_session_mgr(self):
        if self._experiment_session_mgr is None:
            from aero.agent.session import SessionManager

            self._experiment_session_mgr = SessionManager(
                self._project_dir / ".aero" / "experiment-sessions"
            )
        return self._experiment_session_mgr

    def _get_paper_version_mgr(self) -> PaperVersionManager:
        # Paper history always belongs to the project, even while an experiment is active.
        return PaperVersionManager(self._project_dir)

    async def _handle_paper_command(self, text: str) -> None:
        parts = text.strip().split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else ""
        argument = parts[2].strip() if len(parts) > 2 else ""
        manager = self._get_paper_version_mgr()
        usage = (
            "### 论文版本\n\n"
            "- `/paper init`：创建并绑定 `paper/main.md`，保存初始版本\n"
            "- `/paper status`：查看是否有未保存变化\n"
            "- `/paper save 版本说明`：保存当前版本\n"
            "- `/paper versions`：查看版本历史；加 `all` 显示保护版本\n"
            "- `/paper diff [版本ID]`：逐行查看变化\n"
            "- `/paper restore 版本ID`：确认后恢复正文\n"
            "- `/paper export latex|word|pdf`：导出论文文档"
        )
        try:
            if not action:
                self._show_checkpoint_message(usage, force_scroll=True)
                return
            if action == "init":
                if argument:
                    self._show_checkpoint_message(
                        "`/paper init` 不接受路径参数，论文正文固定为 `paper/main.md`。",
                        force_scroll=True,
                    )
                    return
                result = manager.initialize()
                created = "并已保存初始版本" if result.get("created") else ""
                self._show_checkpoint_message(
                    "### 论文版本管理已启用\n\n"
                    f"正文：`{result['document']}`  {created}\n\n"
                    "之后只有固定的 `paper/main.md` 会进入版本历史，其他文件不受影响。",
                    force_scroll=True,
                )
                return
            if action == "status":
                status = manager.status()
                if not status.get("initialized"):
                    self._show_checkpoint_message(
                        "尚未初始化论文正文。使用 `/paper init` 开始。",
                        force_scroll=True,
                    )
                    return
                head = status.get("head") or {}
                change_text = "有未保存变化" if status["changed"] else "与当前版本一致"
                self._show_checkpoint_message(
                    "### 论文版本状态\n\n"
                    f"- 正文：`{status['document']}`\n"
                    f"- 当前版本：`{head.get('id', '无')}` {head.get('title', '')}\n"
                    f"- 状态：**{change_text}**\n"
                    f"- 已保存版本：{status['version_count']} 个",
                    force_scroll=True,
                )
                return
            if action == "save":
                version = manager.save(argument)
                if version.get("created"):
                    message = (
                        "### 论文版本已保存\n\n"
                        f"`{version['id']}`  **{version['title']}**"
                    )
                else:
                    message = (
                        "论文正文没有变化，未重复创建版本。\n\n"
                        f"当前版本：`{version['id']}`  **{version['title']}**"
                    )
                self._show_checkpoint_message(message, force_scroll=True)
                return
            if action == "versions":
                include_safety = argument.lower() == "all"
                if argument and not include_safety:
                    self._show_checkpoint_message(
                        "用法：`/paper versions` 或 `/paper versions all`。",
                        force_scroll=True,
                    )
                    return
                versions = manager.list(include_safety=include_safety)
                if not versions:
                    self._show_checkpoint_message("还没有论文版本。", force_scroll=True)
                    return
                lines = ["### 论文版本历史", ""]
                for item in versions:
                    created = datetime.fromtimestamp(item["created_at"]).astimezone()
                    safety = " · 恢复保护" if item.get("kind") == "pre-restore" else ""
                    lines.append(
                        f"- `{item['id']}`  **{item['title']}** · "
                        f"{created.strftime('%Y-%m-%d %H:%M:%S')}{safety}"
                    )
                lines.extend(["", "使用 `/paper diff 版本ID` 查看变化，或 `/paper restore 版本ID` 恢复正文。"])
                self._show_checkpoint_message("\n".join(lines), force_scroll=True)
                return
            if action == "diff":
                difference = manager.diff(argument or None)
                if not difference.changed:
                    message = (
                        "论文正文与所选版本一致。\n\n"
                        f"版本：`{difference.version_id}`"
                    )
                else:
                    diff_text = difference.unified_diff
                    if len(diff_text) > 24000:
                        diff_text = diff_text[:24000].rstrip() + "\n...（差异过长，已截断）"
                    message = (
                        "### 论文逐行变化\n\n"
                        f"版本：`{difference.version_id}` · 新增 {difference.added_lines} 行 · "
                        f"删除 {difference.removed_lines} 行\n\n"
                        f"```diff\n{diff_text}\n```"
                    )
                self._show_checkpoint_message(message, force_scroll=True)
                return
            if action == "restore":
                if not argument:
                    self._show_checkpoint_message(
                        "用法：`/paper restore 版本ID`。", force_scroll=True
                    )
                    return
                target = manager.load(argument)
                if target is None:
                    raise ValueError(f"找不到论文版本：{argument}")
                difference = manager.diff(argument)
                message = (
                    f"将把 `{difference.document}` 恢复到：\n\n"
                    f"`{target['id']}`  **{target['title']}**\n\n"
                    f"当前正文将变化：新增 {difference.added_lines} 行，删除 {difference.removed_lines} 行。\n\n"
                    "只会覆盖这份 Markdown 正文，其他文件不会改变。"
                    "当前正文若含未保存变化，会先自动保存为恢复保护版本。"
                )
                self.screen.add_class("confirming")
                try:
                    choice = await self.push_screen_wait(
                        ConfirmScreen(
                            message,
                            self.config.language,
                            title="恢复论文版本",
                            allow_label="恢复到此版",
                            deny_label="取消",
                            show_always=False,
                        )
                    )
                finally:
                    self.screen.remove_class("confirming")
                if _normalize_confirm_choice(choice) not in {"approve", "allow", "always"}:
                    self._show_checkpoint_message("已取消恢复论文版本。", force_scroll=True)
                    return
                result = manager.restore(argument)
                restored = result["restored"]
                protection = result.get("protection_version")
                protection_text = (
                    f"\n\n恢复前内容已保存为保护版本：`{protection['id']}`"
                    if protection
                    else ""
                )
                self._show_checkpoint_message(
                    "### 论文版本已恢复\n\n"
                    f"正文：`{result['document']}`\n\n"
                    f"版本：`{restored['id']}`  **{restored['title']}**"
                    f"{protection_text}",
                    force_scroll=True,
                )
                return
            if action in {"export", "latex"}:
                if action == "latex" and argument:
                    self._show_checkpoint_message(
                        "用法：`/paper export latex|word|pdf`。",
                        force_scroll=True,
                    )
                    return
                output_format = "latex" if action == "latex" else argument.lower()
                if output_format not in {"latex", "word", "pdf"}:
                    self._show_checkpoint_message(
                        "用法：`/paper export latex|word|pdf`。",
                        force_scroll=True,
                    )
                    return
                from aero.agent.runtime import Runtime

                env = Runtime._build_exec_env()
                try:
                    result = export_paper(
                        self._project_dir,
                        output_format,
                        env=env,
                    )
                except PaperExportError as exc:
                    if not exc.missing_tools:
                        raise
                    tools_label = "、".join(
                        tool.title() for tool in exc.missing_tools
                    )
                    self.screen.add_class("confirming")
                    try:
                        choice = await self.push_screen_wait(
                            ConfirmScreen(
                                f"导出 {output_format.upper()} 需要 {tools_label}。"
                                "是否安装到 aero-agent 运行环境？",
                                self.config.language,
                                title="安装论文转换组件",
                                allow_label="安装并继续",
                                deny_label="取消",
                                show_always=False,
                            )
                        )
                    finally:
                        self.screen.remove_class("confirming")
                    if _normalize_confirm_choice(choice) not in {
                        "approve",
                        "allow",
                        "always",
                    }:
                        self._show_checkpoint_message(
                            "已取消 LaTeX 导出。", force_scroll=True
                        )
                        return
                    from aero.toolbox.tools.runtime import ensure_runtime_tools

                    self._set_footer_status(f"正在安装 {tools_label}…")
                    await asyncio.sleep(0)
                    installed = await ensure_runtime_tools(exc.missing_tools)
                    if installed.get("status") != "success":
                        self._set_footer_status("论文转换组件安装失败")
                        raise PaperExportError(
                            str(installed.get("message") or "论文转换组件安装失败。")
                        )
                    self._set_footer_status("转换组件安装完成，正在导出论文…")
                    await asyncio.sleep(0)
                    env = Runtime._build_exec_env()
                    result = export_paper(
                        self._project_dir,
                        output_format,
                        env=env,
                    )
                format_labels = {"latex": "LaTeX", "word": "Word", "pdf": "PDF"}
                format_label = format_labels[output_format]
                self._set_footer_status(f"{format_label} 论文已生成")
                self._show_checkpoint_message(
                    f"### {format_label} 论文已生成\n\n"
                    f"源文件：`{result['source']}`\n\n"
                    f"输出文件：`{result['output']}`",
                    force_scroll=True,
                )
                return
            self._show_checkpoint_message(usage, force_scroll=True)
        except Exception as exc:
            if action in {"export", "latex"}:
                self._set_footer_status("论文导出失败")
            self._show_checkpoint_message(f"论文版本操作失败：{exc}", force_scroll=True)

    def _resume_latest_project_session(self) -> bool:
        latest = self._get_session_mgr().latest_session(self._project_dir)
        if latest is None:
            message = t("app.session_no_history", self.config.language)
            self._set_footer_status(message)
            self.notify(message, severity="warning", timeout=3)
            return False
        self._do_load_session(latest.id)
        return True

    async def _handle_checkpoint_command(self, text: str) -> None:
        name = text.split(maxsplit=1)[1].strip() if " " in text.strip() else ""
        if not name:
            self._set_footer_status("正在生成检查点标题…")
            name = await self._generate_checkpoint_title()
        try:
            checkpoint = self._create_checkpoint(name=name, kind="manual")
            exact = sum(1 for item in checkpoint["files"] if item["restore"] == "exact")
            references = len(checkpoint["files"]) - exact
            capability = (
                "支持文件恢复" if checkpoint["exact_restore"] else "仅保存数据清单"
            )
            active_experiment = self._get_experiment_mgr().active()
            owner = (
                f"实验：{active_experiment['name']}"
                if active_experiment is not None
                else "主流程"
            )
            message = (
                "### 检查点已创建\n\n"
                f"**{checkpoint['name']}**  `{checkpoint['id']}`\n\n"
                f"- 所属：{owner}\n"
                f"- 可恢复文件：{exact} 个\n"
                f"- 仅记录数据：{references} 个\n"
                f"- 恢复能力：{capability}"
            )
            if checkpoint.get("git_error"):
                message += f"\n\n> {checkpoint['git_error']}"
            self._show_checkpoint_message(message)
        except Exception as exc:
            self._show_checkpoint_message(f"检查点创建失败：{exc}")

    async def _generate_checkpoint_title(self) -> str:
        messages = self.agent.messages if self.agent is not None else []
        fallback = _checkpoint_title_from_messages(messages) or "当前工作进度"
        client = None
        try:
            from aero.agent.llm_client import LLMConfig, LLMClient

            llm_cfg = LLMConfig(
                provider=self.config.llm.provider,
                model=self.config.llm.model,
                api_key=self.config.llm.active_api_key(),
                base_url=self.config.llm.base_url,
            )
            client = LLMClient(llm_cfg)
            prompt = _checkpoint_title_prompt(messages, self.config.language)
            title = await client.chat([Message(role="user", content=prompt)])
            return _normalize_checkpoint_title(title) or fallback
        except Exception as exc:
            debug_log("tui.checkpoint_title_generation_failed", error=str(exc))
            return fallback
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception as exc:
                    debug_log("tui.checkpoint_title_client_close_failed", error=str(exc))

    def _handle_checkpoints_command(self, text: str = "/checkpoints") -> None:
        argument = text.split(maxsplit=1)[1].strip().lower() if " " in text.strip() else ""
        if argument not in {"", "all"}:
            self._show_checkpoint_message("用法：`/checkpoints` 或 `/checkpoints all`。")
            return
        all_checkpoints = self._get_checkpoint_mgr().list()
        active_experiment = self._get_experiment_mgr().active()
        scope_name = (
            f"实验“{active_experiment['name']}”"
            if active_experiment is not None
            else "主流程"
        )
        safety = [item for item in all_checkpoints if item.get("kind") == "pre-restore"]
        checkpoints = all_checkpoints if argument == "all" else [
            item for item in all_checkpoints if item.get("kind") != "pre-restore"
        ]
        if not checkpoints:
            message = (
                f"{scope_name}还没有用户创建的检查点。"
                "使用 `/checkpoint 名称` 创建一个。"
            )
            if safety:
                message += f"\n\n另有 {len(safety)} 条恢复保护记录，可用 `/checkpoints all` 查看。"
            self._show_checkpoint_message(message)
            return
        title = (
            f"{scope_name}全部检查点"
            if argument == "all"
            else f"{scope_name}检查点"
        )
        lines = [f"### {title}", ""]
        for item in checkpoints:
            lines.append(_format_checkpoint_list_entry(item))
        if safety and argument != "all":
            lines.extend(
                [
                    "",
                    f"已隐藏 {len(safety)} 条恢复保护记录；使用 `/checkpoints all` 查看。",
                ]
            )
        lines.extend(["", "使用 `/restore 检查点名称或ID` 预览差异并恢复。"])
        self._show_checkpoint_message("\n".join(lines))

    async def _handle_clear_checkpoints_command(self, text: str) -> None:
        if text.strip() != "/checkpoints clear":
            self._show_checkpoint_message("用法：`/checkpoints clear`。")
            return
        manager = self._get_checkpoint_mgr()
        checkpoints = manager.list()
        if not checkpoints:
            self._show_checkpoint_message("当前没有可清理的检查点。")
            return

        safety_count = sum(
            1 for item in checkpoints if item.get("kind") == "pre-restore"
        )
        manual_count = len(checkpoints) - safety_count
        confirmation_message = (
            "清理全部检查点后：\n\n"
            f"- 用户检查点：{manual_count} 个\n"
            f"- 恢复保护记录：{safety_count} 个\n\n"
            "所有检查点快照和恢复记录都会永久删除。"
            "项目文件、data/、会话历史和配置不会受到影响。"
        )
        self.screen.add_class("confirming")
        try:
            choice = await self.push_screen_wait(
                ConfirmScreen(
                    confirmation_message,
                    self.config.language,
                    title=t("confirm.checkpoints_clear_title", self.config.language),
                    allow_label=t("confirm.checkpoints_clear", self.config.language),
                    deny_label=t("confirm.cancel", self.config.language),
                    show_always=False,
                )
            )
        finally:
            self.screen.remove_class("confirming")
        if _normalize_confirm_choice(choice) not in {"approve", "allow", "always"}:
            self._show_checkpoint_message("已取消清理，检查点保持不变。")
            return

        try:
            removed = manager.clear()
            self._show_checkpoint_message(f"已清理全部 {len(removed)} 个检查点。")
        except Exception as exc:
            self._show_checkpoint_message(f"检查点清理失败：{exc}")

    async def _handle_delete_checkpoint_command(self, text: str) -> None:
        parts = text.split(maxsplit=2)
        checkpoint_id = parts[2].strip() if len(parts) > 2 else ""
        if not checkpoint_id:
            self._show_checkpoint_message(
                "用法：`/checkpoint delete 检查点ID`。先用 `/checkpoints all` 查看列表。"
            )
            return
        manager = self._get_checkpoint_mgr()
        try:
            checkpoint = manager.load(checkpoint_id)
            if checkpoint is None:
                raise ValueError(f"找不到检查点：{checkpoint_id}")
        except Exception as exc:
            self._show_checkpoint_message(f"无法读取检查点：{exc}")
            return

        exact = sum(1 for item in checkpoint.get("files", []) if item.get("restore") == "exact")
        references = len(checkpoint.get("files", [])) - exact
        kind = "恢复保护记录" if checkpoint.get("kind") == "pre-restore" else "用户检查点"
        message = "\n".join(
            [
                f"永久删除{kind}：{checkpoint['name']}",
                f"ID：{checkpoint['id']}",
                f"可恢复文件：{exact}，仅记录数据：{references}",
                "",
                "项目文件和 data/ 数据不会被删除，但该检查点将无法再恢复。",
            ]
        )
        self.screen.add_class("confirming")
        try:
            choice = await self.push_screen_wait(
                ConfirmScreen(
                    message,
                    self.config.language,
                    title=t("confirm.checkpoint_delete_title", self.config.language),
                    allow_label=t("confirm.checkpoint_delete", self.config.language),
                    deny_label=t("confirm.cancel", self.config.language),
                    show_always=False,
                )
            )
        finally:
            self.screen.remove_class("confirming")
        if _normalize_confirm_choice(choice) not in {"approve", "allow", "always"}:
            self._show_checkpoint_message("已取消删除，检查点保持不变。")
            return
        try:
            deleted = manager.delete(checkpoint["id"])
            self._show_checkpoint_message(
                f"已删除检查点 **{deleted['name']}**（`{deleted['id']}`）。"
            )
        except Exception as exc:
            self._show_checkpoint_message(f"检查点删除失败：{exc}")

    def _handle_rename_checkpoint_command(self, text: str) -> None:
        parts = text.split(maxsplit=3)
        checkpoint_id = parts[2].strip() if len(parts) > 2 else ""
        new_name = parts[3].strip() if len(parts) > 3 else ""
        if not checkpoint_id or not new_name:
            self._show_checkpoint_message(
                "用法：`/checkpoint rename 检查点ID 新名称`。"
                "先用 `/checkpoints all` 查看列表。"
            )
            return
        try:
            manager = self._get_checkpoint_mgr()
            previous = manager.load(checkpoint_id)
            if previous is None:
                raise ValueError(f"找不到检查点：{checkpoint_id}")
            renamed = manager.rename(previous["id"], new_name)
            self._show_checkpoint_message(
                "### 检查点已改名\n\n"
                f"`{renamed['id']}`  **{previous['name']}** → **{renamed['name']}**"
            )
        except Exception as exc:
            self._show_checkpoint_message(f"检查点改名失败：{exc}")

    async def _handle_restore_command(self, text: str) -> None:
        target = text.split(maxsplit=1)[1].strip() if " " in text.strip() else ""
        if not target:
            self._show_checkpoint_message(
                "用法：`/restore 检查点ID`。先用 `/checkpoints` 查看列表。"
            )
            return
        from aero.checkpoints import CheckpointError

        manager = self._get_checkpoint_mgr()
        try:
            checkpoint = manager.load(target)
            if checkpoint is None:
                raise CheckpointError(f"找不到检查点：{target}")
            diff = manager.diff(checkpoint["id"])
        except Exception as exc:
            self._show_checkpoint_message(f"无法读取检查点：{exc}")
            return

        self._show_checkpoint_message(_format_checkpoint_diff(checkpoint, diff))
        if not checkpoint.get("exact_restore"):
            return
        confirmation_message = _restore_confirmation_message(checkpoint, diff)
        self.screen.add_class("confirming")
        try:
            choice = await self.push_screen_wait(
                ConfirmScreen(
                    confirmation_message,
                    self.config.language,
                    title=t("confirm.checkpoint_restore_title", self.config.language),
                    allow_label=t("confirm.checkpoint_restore", self.config.language),
                    deny_label=t("confirm.cancel", self.config.language),
                    show_always=False,
                )
            )
        finally:
            self.screen.remove_class("confirming")
        if _normalize_confirm_choice(choice) not in {"approve", "allow", "always"}:
            self._show_checkpoint_message("已取消恢复，没有修改文件。")
            return

        try:
            self._create_checkpoint(
                name=f"恢复前安全检查点：{checkpoint['name']}", kind="pre-restore"
            )
            restored = manager.restore(checkpoint["id"])
            self._restore_checkpoint_session(restored)
            self._show_checkpoint_message(
                f"已恢复检查点 **{restored['name']}**。大数据仍保持当前位置和内容；"
                "后续操作将作为独立的恢复后进度继续保存。"
            )
        except Exception as exc:
            self._show_checkpoint_message(f"检查点恢复失败：{exc}")

    async def _handle_experiment_command(self, text: str) -> None:
        parts = text.strip().split(maxsplit=2)
        if len(parts) == 1:
            self._show_checkpoint_message(
                "用法：`/experiment new 实验名称` 创建，`/experiment finish` 完成，"
                "或 `/experiment delete 实验ID` 删除。"
            )
            return
        action = parts[1].lower()
        if action == "finish":
            self._start_finishing_active_experiment()
            return
        if action == "delete":
            target = parts[2].strip() if len(parts) > 2 else ""
            await self._delete_experiment(target)
            return
        name = (
            parts[2].strip()
            if action == "new" and len(parts) > 2
            else " ".join(parts[1:])
        )
        if not name:
            self._show_checkpoint_message("请输入实验名称，例如：`/experiment new 臭氧敏感性`。")
            return
        self._start_experiment(name)

    async def _handle_experiments_command(self, text: str) -> None:
        parts = text.strip().split(maxsplit=2)
        if len(parts) == 1:
            self._show_experiment_list()
            return
        if parts[1].lower() == "clear":
            await self._clear_experiments()
            return
        if parts[1].lower() != "switch" or len(parts) < 3:
            self._show_checkpoint_message(
                "用法：`/experiments` 查看列表，`/experiments switch 实验ID` 切换，"
                "或 `/experiments clear` 清理全部实验。"
            )
            return
        self._switch_experiment(parts[2].strip())

    def _start_finishing_active_experiment(self) -> None:
        experiment = self._get_experiment_mgr().active()
        if experiment is None:
            self._show_checkpoint_message("当前没有正在进行的实验。")
            return
        if (
            self._experiment_finish_worker is not None
            and self._experiment_finish_worker.is_running
        ):
            self._show_checkpoint_message(
                f"正在总结实验 **{experiment['name']}**，请稍候。",
                force_scroll=True,
            )
            return
        self._show_checkpoint_message(
            f"正在总结实验 **{experiment['name']}** 并生成文字报告…",
            force_scroll=True,
        )
        self._set_footer_status(f"正在完成实验：{experiment['name']}")
        debug_log(
            "tui.experiment_finish_started",
            experiment_id=experiment["id"],
            experiment_name=experiment["name"],
        )
        self._experiment_finish_worker = self.run_worker(
            self._finish_active_experiment(experiment["id"]),
            exclusive=False,
            group="experiment-finish",
        )

    async def _delete_experiment(self, target: str) -> None:
        if not target:
            self._show_checkpoint_message(
                "用法：`/experiment delete 实验ID`。先用 `/experiments` 查看列表。"
            )
            return
        manager = self._get_experiment_mgr()
        try:
            experiment = manager.load(target)
            if experiment is None:
                raise ValueError(f"找不到实验：{target}")
            workspace = manager.workspace_path(experiment)
            file_count = len(manager.artifacts(experiment))
            from aero.checkpoints import CheckpointManager

            checkpoint_count = len(
                CheckpointManager(
                    workspace, experiment_id=experiment["id"]
                ).list()
            )
        except Exception as exc:
            self._show_checkpoint_message(f"无法读取实验：{exc}")
            return
        message = (
            f"永久删除实验“{experiment['name']}”后：\n\n"
            f"- 实验目录：{workspace.relative_to(self._project_dir)}\n"
            f"- 目录内文件：{file_count} 个\n"
            f"- 实验检查点：{checkpoint_count} 个\n\n"
            "实验文件、报告、元数据和专属对话都会被删除，且无法恢复。"
        )
        if not await self._confirm_experiment_removal(
            message,
            title=t("confirm.experiment_delete_title", self.config.language),
            allow_label=t("confirm.experiment_delete", self.config.language),
        ):
            self._show_checkpoint_message("已取消删除，实验保持不变。")
            return
        active = manager.active()
        deleting_active = active is not None and active["id"] == experiment["id"]
        try:
            if deleting_active:
                self._save_experiment_slot(experiment["id"], experiment["name"])
            deleted = manager.delete(experiment["id"])
            self._get_experiment_session_mgr().delete(experiment["id"])
            if deleting_active:
                next_active = manager.active()
                self._load_experiment_slot(
                    next_active["id"] if next_active else "__main__"
                )
                if self.agent is not None:
                    self.agent.reset_system_prompt(self.config.language)
            self._show_checkpoint_message(f"已删除实验 **{deleted['name']}**。")
        except Exception as exc:
            self._show_checkpoint_message(f"实验删除失败：{exc}")

    async def _clear_experiments(self) -> None:
        manager = self._get_experiment_mgr()
        experiments = manager.list()
        if not experiments:
            self._show_checkpoint_message("当前没有可清理的实验。")
            return
        file_count = sum(len(manager.artifacts(item)) for item in experiments)
        from aero.checkpoints import CheckpointManager

        checkpoint_count = sum(
            len(
                CheckpointManager(
                    manager.workspace_path(item), experiment_id=item["id"]
                ).list()
            )
            for item in experiments
        )
        message = (
            f"清理全部实验后：\n\n- 实验：{len(experiments)} 个\n"
            f"- 实验目录内文件：{file_count} 个\n"
            f"- 实验检查点：{checkpoint_count} 个\n\n"
            "所有实验目录、报告、元数据和实验专属对话都会被永久删除。"
            "主项目文件、检查点和普通聊天历史不会受到影响。"
        )
        if not await self._confirm_experiment_removal(
            message,
            title=t("confirm.experiments_clear_title", self.config.language),
            allow_label=t("confirm.experiments_clear", self.config.language),
        ):
            self._show_checkpoint_message("已取消清理，实验保持不变。")
            return
        try:
            removed = manager.clear()
            session_manager = self._get_experiment_session_mgr()
            for experiment in removed:
                session_manager.delete(experiment["id"])
            self._load_experiment_slot("__main__")
            if self.agent is not None:
                self.agent.reset_system_prompt(self.config.language)
            self._show_checkpoint_message(f"已清理全部 {len(removed)} 个实验。")
        except Exception as exc:
            self._show_checkpoint_message(f"实验清理失败：{exc}")

    async def _confirm_experiment_removal(
        self, message: str, *, title: str, allow_label: str
    ) -> bool:
        self.screen.add_class("confirming")
        try:
            choice = await self.push_screen_wait(
                ConfirmScreen(
                    message,
                    self.config.language,
                    title=title,
                    allow_label=allow_label,
                    deny_label=t("confirm.cancel", self.config.language),
                    show_always=False,
                )
            )
        finally:
            self.screen.remove_class("confirming")
        return _normalize_confirm_choice(choice) in {"approve", "allow", "always"}

    def _start_experiment(self, name: str) -> None:
        try:
            manager = self._get_experiment_mgr()
            current = manager.active()
            source_slot = current["id"] if current else "__main__"
            self._save_experiment_slot(
                source_slot, current["name"] if current else "主项目"
            )
            base_messages = copy.deepcopy(self.agent.messages) if self.agent is not None else []
            experiment = manager.create(
                name,
                base_checkpoint=self._get_checkpoint_mgr().current_checkpoint_id(),
            )
            self._save_experiment_slot(
                experiment["id"], experiment["name"], messages=base_messages
            )
            if self.agent is not None:
                self.agent.reset_system_prompt(self.config.language)
            workspace = manager.workspace_path(experiment).relative_to(self._project_dir)
            self._show_checkpoint_message(
                f"### 实验已创建\n\n**{experiment['name']}**  `{experiment['id']}`\n\n"
                f"工作目录：`{workspace}`\n\n"
                "后续脚本、图片、计划、输出和临时文件会优先写入该实验目录。"
            )
        except Exception as exc:
            self._show_checkpoint_message(f"无法创建实验：{exc}")

    def _show_experiment_list(self) -> None:
        manager = self._get_experiment_mgr()
        experiments = manager.list()
        active = manager.active()
        if not experiments:
            self._show_checkpoint_message(
                "还没有实验。使用 `/experiment new 实验名称` 创建一个。",
                force_scroll=True,
            )
            return
        lines = ["### 实验", ""]
        for item in experiments:
            marker = " **当前**" if active and item["id"] == active["id"] else ""
            status = "已完成" if item.get("status") == "completed" else "进行中"
            timestamp = datetime.fromtimestamp(item["updated_at"]).strftime(
                "%Y-%m-%d %H:%M"
            )
            lines.append(
                f"- `{item['id']}`  **{item['name']}** · {status} · {timestamp}{marker}"
            )
        lines.extend(
            [
                "",
                "使用 `/experiments switch 实验ID` 切换；使用 `main` 返回主项目；"
                "使用 `/experiment delete 实验ID` 删除。",
            ]
        )
        self._show_checkpoint_message("\n".join(lines), force_scroll=True)

    def _switch_experiment(self, target: str) -> None:
        try:
            manager = self._get_experiment_mgr()
            current = manager.active()
            source_slot = current["id"] if current else "__main__"
            self._save_experiment_slot(
                source_slot, current["name"] if current else "主项目"
            )
            if target.lower() in {"main", "主项目"}:
                manager.leave()
                selected = None
                target_slot = "__main__"
            else:
                selected = manager.switch(target)
                target_slot = selected["id"]
            self._load_experiment_slot(target_slot)
            if self.agent is not None:
                self.agent.reset_system_prompt(self.config.language)
            label = selected["name"] if selected else "主项目"
            self._show_checkpoint_message(f"已切换到 **{label}**。")
        except Exception as exc:
            self._show_checkpoint_message(f"无法切换实验：{exc}")

    async def _finish_active_experiment(self, experiment_id: str) -> None:
        try:
            manager = self._get_experiment_mgr()
            experiment = manager.active()
            if experiment is None or experiment["id"] != experiment_id:
                self._show_checkpoint_message(
                    "实验已被切换，已停止生成完成报告。", force_scroll=True
                )
                return
            self._save_experiment_slot(experiment["id"], experiment["name"])
            messages = (
                copy.deepcopy(self.agent.messages) if self.agent is not None else []
            )
            artifacts = manager.artifacts(experiment)
            report = await self._generate_experiment_report(
                experiment, messages, artifacts
            )
            completed = manager.complete(experiment["id"], report)
            finish_checkpoint = None
            finish_checkpoint_error = ""
            try:
                finish_checkpoint = self._create_experiment_finish_checkpoint(completed)
            except Exception as exc:
                finish_checkpoint_error = str(exc)
                debug_log(
                    "tui.experiment_finish_checkpoint_failed",
                    experiment_id=completed["id"],
                    error=repr(exc),
                )
            report_path = completed["report"]
            completion_message = (
                f"### 实验已完成\n\n**{completed['name']}**\n\n"
                f"报告已保存到 [{report_path}]({report_path})。"
                "当前仍停留在该实验，可继续查看历史或补充分析；"
                "需要离开时使用 `/experiments switch main`。\n\n"
                f"{report}"
            )
            if finish_checkpoint is not None:
                completion_message += (
                    "\n\n主流程检查点已创建："
                    f"**{finish_checkpoint['name']}** `{finish_checkpoint['id']}`"
                )
            elif finish_checkpoint_error:
                completion_message += (
                    f"\n\n> 主流程实验结束检查点创建失败：{finish_checkpoint_error}"
                )
            if self.agent is not None:
                self.agent.messages.append(
                    Message(
                        role="assistant",
                        content=(
                            f"[实验完成摘要]\n实验：{completed['name']}\n"
                            f"报告：{report_path}\n\n{report}"
                        ),
                    )
                )
                self._save_experiment_slot(completed["id"], completed["name"])
            self._show_checkpoint_message(completion_message, force_scroll=True)
        except Exception as exc:
            debug_log("tui.experiment_finish_failed", error=repr(exc))
            self._show_checkpoint_message(
                f"无法完成实验：{exc}", force_scroll=True
            )
        finally:
            self._experiment_finish_worker = None
            self._refresh_model_info()

    def _create_experiment_finish_checkpoint(self, experiment: dict) -> dict:
        session_snapshot = self._get_experiment_session_mgr().snapshot("__main__")
        if session_snapshot is None and self._session_id:
            session_snapshot = self._get_session_mgr().snapshot(self._session_id)
        model = {
            "provider": self.config.llm.provider,
            "model": self.config.llm.model,
            "vision_model": self.config.vision.model,
            "mode": self.config.mode,
        }
        return self._get_main_checkpoint_mgr().create(
            f"实验结束：{experiment['name']}",
            session_id=self._session_id,
            session_snapshot=session_snapshot,
            model=model,
            tool_ledger=[],
            kind="experiment-finish",
            related_experiment={
                "id": experiment["id"],
                "name": experiment["name"],
                "report": experiment.get("report") or "",
            },
        )

    async def _generate_experiment_report(
        self, experiment: dict, messages: list[Message], artifacts: list[str]
    ) -> str:
        fallback = _fallback_experiment_report(experiment, messages, artifacts)
        from aero.data.memos import render_memo_context

        memo_context = render_memo_context(self._project_dir)
        client = None
        try:
            from aero.agent.llm_client import LLMConfig, LLMClient

            client = LLMClient(
                LLMConfig(
                    provider=self.config.llm.provider,
                    model=self.config.llm.model,
                    api_key=self.config.llm.active_api_key(),
                    base_url=self.config.llm.base_url,
                )
            )
            report = await client.chat(
                [
                    Message(
                        role="user",
                        content=_experiment_report_prompt(
                            experiment, messages, artifacts, memo_context
                        ),
                    )
                ]
            )
            return report.strip() or fallback
        except Exception as exc:
            debug_log("tui.experiment_report_generation_failed", error=str(exc))
            return fallback
        finally:
            if client is not None:
                with suppress(Exception):
                    await client.close()

    def _save_experiment_slot(
        self,
        slot_id: str,
        name: str,
        *,
        messages: list[Message] | None = None,
    ) -> None:
        if self.agent is None:
            return
        from aero.agent.session import SessionMeta

        saved_messages = messages if messages is not None else self.agent.messages
        meta = SessionMeta(
            id=slot_id,
            name=name,
            message_count=len(saved_messages),
            tracker=self.agent.tracker.to_dict(),
            model=self.config.llm.model,
            provider=self.config.llm.provider,
            vision_model=self.config.vision.model,
            mode=self.config.mode,
            title_source="experiment",
            project_dir=str(self._project_dir),
            transcript=self._chat_transcript(),
        )
        self._get_experiment_session_mgr().save(slot_id, saved_messages, meta)

    def _load_experiment_slot(self, slot_id: str) -> None:
        if self.agent is None:
            self._init_agent()
        loaded = self._get_experiment_session_mgr().load(slot_id)
        if self.agent is None:
            return
        if loaded is None:
            system_message = self.agent.messages[0] if self.agent.messages else None
            self.agent.messages = [system_message] if system_message is not None else []
            self.agent.tracker = TokenTracker()
            transcript = []
        else:
            messages, meta = loaded
            self.agent.messages = messages
            self.agent.tracker = TokenTracker.from_dict(meta.tracker)
            transcript = meta.transcript
        chat = self.query_one("#chat-area", VerticalScroll)
        chat.remove_children()
        self._mount_chat_title()
        self._render_loaded_session_messages(self.agent.messages, transcript)
        self._agent_msg = None
        self._status_msg = None
        self._status_sessions = []
        self._enter_chat_mode()

    def _create_checkpoint(self, *, name: str, kind: str) -> dict:
        self._auto_save_session()
        session_snapshot = None
        active_experiment = self._get_experiment_mgr().active()
        checkpoint_session_id = self._session_id
        if active_experiment is not None:
            checkpoint_session_id = active_experiment["id"]
            session_snapshot = self._get_experiment_session_mgr().snapshot(
                active_experiment["id"]
            )
        elif self._session_id:
            session_snapshot = self._get_session_mgr().snapshot(self._session_id)
        model = {
            "provider": self.config.llm.provider,
            "model": self.config.llm.model,
            "vision_model": self.config.vision.model,
            "mode": self.config.mode,
        }
        ledger = _checkpoint_tool_ledger(self.agent.messages if self.agent else [])
        return self._get_checkpoint_mgr().create(
            name,
            session_id=checkpoint_session_id,
            session_snapshot=session_snapshot,
            model=model,
            tool_ledger=ledger,
            kind=kind,
        )

    def _restore_checkpoint_session(self, checkpoint: dict) -> None:
        snapshot = self._get_checkpoint_mgr().session_snapshot(checkpoint["id"])
        if snapshot is None or self.agent is None:
            return
        import uuid

        messages, meta = self._get_session_mgr().load_snapshot(snapshot)
        self.agent.messages = messages
        self.agent.tracker = TokenTracker.from_dict(meta.tracker)
        self._session_id = uuid.uuid4().hex[:12]
        set_session_id(self._session_id)
        self._pending_session_title = f"{meta.name}（恢复）" if meta.name else "恢复的会话"
        self._session_saved_on_exit = False
        chat = self.query_one("#chat-area", VerticalScroll)
        chat.remove_children()
        self._mount_chat_title()
        self._render_loaded_session_messages(messages, meta.transcript)

    def _show_checkpoint_message(
        self, message: str, *, force_scroll: bool = False
    ) -> None:
        if force_scroll:
            self._user_scrolled_up = False
        self._enter_chat_mode()
        self._mount_agent_message_sync(message, force_scroll=force_scroll)
        self._chat_log.append(f"Aero:\n{message}")
        self._last_reply_text = message
        self._maybe_scroll_to_end()
        if force_scroll:
            self._force_scroll_chat_to_end()
            self.call_after_refresh(self._force_scroll_chat_to_end)
            self.set_timer(0.05, self._force_scroll_chat_to_end)

    def _handle_session_command(self, text: str) -> None:
        parts = text.split(maxsplit=2)
        if len(parts) >= 2 and parts[1].strip().lower() == "rename":
            title = parts[2].strip() if len(parts) > 2 else ""
            self._rename_current_session(title)
            return
        self._show_session_picker()

    def _handle_new_session_command(self, text: str) -> None:
        title = text.split(maxsplit=1)[1].strip() if " " in text.strip() else ""
        self._start_new_session(title)

    def _start_new_session(self, title: str = "") -> None:
        self._auto_save_session()
        title = _normalize_generated_session_title(title)
        chat = self.query_one("#chat-area", VerticalScroll)
        chat.remove_children()
        self._mount_chat_title()
        if self.agent is None:
            self._init_agent()
        elif self.agent.messages:
            self.agent.messages = [self.agent.messages[0]]
            self.agent.tracker = TokenTracker()
        self._session_id = None
        set_session_id(None)
        self._pending_session_title = title
        self._session_saved_on_exit = False
        self._agent_msg = None
        self._status_msg = None
        self._status_sessions = []
        self._last_reply_text = ""
        self._image_attachments = []
        self._reset_chat_log_with_title()
        self._enter_chat_mode()
        message = (
            t("app.new_session_named", self.config.language).format(title=title)
            if title
            else t("app.new_session", self.config.language)
        )
        self._set_footer_status(message)
        self._refresh_model_info()
        self._maybe_scroll_to_end()

    def _auto_save_session(self, name: str = "", title_source: str = "") -> None:
        transcript = self._chat_transcript()
        # Local UI interactions (for example API-key setup and `/checkpoints`)
        # only appear in the display transcript. They never enter the Agent
        # context, so persisting them as standalone sessions produces noisy
        # history entries and can expose a redacted fragment as a title.
        if self.agent is None or not _has_persistable_session_messages(self.agent.messages):
            return
        active_experiment = self._get_experiment_mgr().active()
        if active_experiment is not None:
            self._save_experiment_slot(active_experiment["id"], active_experiment["name"])
            return
        mgr = self._get_session_mgr()
        from aero.agent.session import SessionMeta
        import uuid
        sid = self._session_id or uuid.uuid4().hex[:12]
        existing = mgr.load(sid)
        self._session_id = sid
        set_session_id(sid)
        source = title_source
        if name:
            pass
        elif self._pending_session_title:
            name = self._pending_session_title
            source = "manual"
        elif existing is not None and existing[1].name:
            name = existing[1].name
            source = existing[1].title_source
        else:
            name = _session_title_from_messages(self.agent.messages) or sid
            source = "pending"
        meta = SessionMeta(
            id=sid,
            name=name,
            message_count=len(self.agent.messages),
            tracker=self.agent.tracker.to_dict(),
            model=self.config.llm.model,
            provider=self.config.llm.provider,
            vision_model=self.config.vision.model,
            mode=self.config.mode,
            title_source=source,
            project_dir=str(self._project_dir),
            transcript=transcript,
        )
        mgr.save(sid, self.agent.messages, meta)
        if self._pending_session_title and meta.title_source == "manual":
            self._pending_session_title = ""
        if meta.title_source == "pending":
            self._schedule_session_title_generation(sid)

    def _schedule_session_title_generation(self, session_id: str) -> None:
        if session_id in self._session_title_workers:
            return
        self._session_title_workers.add(session_id)
        try:
            self.run_worker(
                self._generate_session_title(session_id),
                exclusive=False,
                group="session-title",
            )
        except Exception as e:
            self._session_title_workers.discard(session_id)
            debug_log("tui.session_title_schedule_failed", error=str(e))

    async def _generate_session_title(self, session_id: str) -> None:
        try:
            mgr = self._get_session_mgr()
            loaded = mgr.load(session_id)
            if loaded is None:
                return
            messages, meta = loaded
            if meta.title_source in {"auto", "manual"}:
                return
            client = None
            try:
                from aero.agent.llm_client import LLMConfig, LLMClient

                llm_cfg = LLMConfig(
                    provider=self.config.llm.provider,
                    model=self.config.llm.model,
                    api_key=self.config.llm.active_api_key(),
                    base_url=self.config.llm.base_url,
                )
                client = LLMClient(llm_cfg)
                prompt = _session_title_prompt(messages, self.config.language)
                title = await client.chat([Message(role="user", content=prompt)])
            except Exception as e:
                debug_log("tui.session_title_generation_failed", session_id=session_id, error=str(e))
                return
            finally:
                if client is not None:
                    await client.close()

            title = _normalize_generated_session_title(title)
            if not title:
                return
            loaded = mgr.load(session_id)
            if loaded is None:
                return
            latest_messages, latest_meta = loaded
            if latest_meta.title_source == "manual":
                return
            latest_meta.name = title
            latest_meta.title_source = "auto"
            mgr.save(session_id, latest_messages, latest_meta)
            if self._session_id == session_id:
                self._set_footer_status(
                    t("app.session_title_updated", self.config.language).format(title=title)
                )
        finally:
            self._session_title_workers.discard(session_id)

    def _rename_current_session(self, title: str) -> None:
        title = _normalize_generated_session_title(title)
        lang = self.config.language
        if not title:
            message = t("app.session_rename_usage", lang)
            self._set_footer_status(message)
            self.notify(message, severity="warning", timeout=2)
            return
        if self.agent is None or len(self.agent.messages) <= 1:
            message = t("app.session_rename_no_session", lang)
            self._set_footer_status(message)
            self.notify(message, severity="warning", timeout=2)
            return
        self._auto_save_session(name=title, title_source="manual")
        message = t("app.session_renamed", lang).format(title=title)
        self._set_footer_status(message)
        self.notify(message, timeout=2)

    def _show_session_picker(self) -> None:
        lang = self.config.language
        mgr = self._get_session_mgr()
        sessions = mgr.list_sessions()
        if not sessions:
            message = t("app.session_no_history", lang)
            self._set_footer_status(message)
            self.notify(message, timeout=2)
            self.query_one("#user-input", TextArea).focus()
            return
        options = []
        for s in sessions:
            label = _session_option_label(s.name, s.updated_at)
            options.append((s.id, label))
        self.push_screen(
            SelectScreen(
                t("app.session_list_title", lang),
                options,
                "" if not sessions else sessions[0].id,
                lang=lang,
                on_delete=self._delete_selected_session,
                on_empty=self._handle_empty_session_list,
                hint=t("select.session_hint", lang),
            ),
            callback=self._apply_selected_session,
        )

    def _delete_selected_session(self, session_id: str) -> bool:
        mgr = self._get_session_mgr()
        if not mgr.delete(session_id):
            message = t("app.session_not_found", self.config.language).format(id=session_id)
            self._set_footer_status(message)
            self.notify(message, severity="warning", timeout=2)
            return False
        if self._session_id == session_id:
            self._session_id = None
            set_session_id(None)
        message = t("app.session_deleted", self.config.language).format(id=session_id)
        self._set_footer_status(message)
        self.notify(message, timeout=2)
        return True

    def _handle_empty_session_list(self) -> None:
        message = t("app.session_no_history", self.config.language)
        self._set_footer_status(message)
        self.notify(message, timeout=2)
        self.query_one("#user-input", TextArea).focus()

    def _apply_selected_session(self, session_id: str | None) -> None:
        if session_id:
            self._do_load_session(session_id)
        self.query_one("#user-input", TextArea).focus()

    def _do_load_session(self, session_id: str) -> None:
        lang = self.config.language
        chat = self.query_one("#chat-area", VerticalScroll)
        mgr = self._get_session_mgr()
        result = mgr.load(session_id)
        if result is None:
            message = t("app.session_not_found", lang).format(id=session_id)
            self._set_footer_status(message)
            self.notify(message, severity="warning", timeout=2)
            return
        messages, meta = result
        if self.agent is None:
            self._init_agent()
        self.agent.messages = messages
        self.agent.tracker = TokenTracker.from_dict(meta.tracker)
        self.config.mode = meta.mode or self.config.mode
        self.agent.config.mode = self.config.mode
        self.agent.reset_system_prompt(lang)
        self._session_id = session_id
        set_session_id(session_id)
        self._pending_session_title = ""
        self._enter_chat_mode()
        chat.remove_children()
        self._mount_chat_title()
        self._render_loaded_session_messages(messages, meta.transcript)
        self._agent_msg = None
        self._status_msg = None
        self._status_sessions.clear()
        self._image_attachments.clear()
        self._refresh_model_info()
        self._refresh_mode_ui()
        self._set_footer_status(
            t("app.session_loaded", lang).format(name=meta.name, id=session_id)
        )
        self.query_one("#user-input", TextArea).focus()

    def _render_loaded_session_messages(
        self,
        messages: list[Message],
        transcript: list[dict[str, str]] | None = None,
    ) -> None:
        if transcript:
            self._render_loaded_transcript(transcript)
            return

        chat = self.query_one("#chat-area", VerticalScroll)
        self._reset_chat_log_with_title()
        self._last_reply_text = ""
        rendered_any = False
        pending_divider = False
        skip_next_ack = False

        for msg in messages:
            if msg.role == "system" or msg.role == "tool":
                continue
            if msg.role == "assistant" and not msg.content.strip():
                continue

            if _is_compact_summary_message(msg):
                summary_text = msg.content.split("\n", 1)[-1].strip()
                self._mount_compact_summary_block(summary_text)
                self._chat_log.append(f"[上下文压缩总结]\n{summary_text}")
                rendered_any = True
                skip_next_ack = True
                continue

            if skip_next_ack and msg.role == "assistant":
                stripped = msg.content.strip()
                if stripped in ("OK, I understand the context above.", "OK, I understand the context above。"):
                    skip_next_ack = False
                    continue
            skip_next_ack = False

            if pending_divider and msg.role == "user":
                chat.mount(Static("─", classes="divider"))
                pending_divider = False

            if msg.role == "user":
                self._mount_user_message_sync(msg.content)
                self._chat_log.append(f"你:\n{msg.content}")
                rendered_any = True
                continue

            if msg.role == "assistant":
                content = _sanitize_user_facing_text(msg.content)
                self._mount_agent_message_sync(content)
                self._chat_log.append(f"Aero:\n{content}")
                self._last_reply_text = content
                rendered_any = True
                pending_divider = True

        if pending_divider:
            chat.mount(Static("─", classes="divider"))
        if not rendered_any:
            chat.mount(Static(f"[dim]{t('app.session_empty_history', self.config.language)}[/dim]"))
        self._maybe_scroll_to_end()

    def _render_loaded_transcript(self, transcript: list[dict[str, str]]) -> None:
        """Rebuild the visible conversation without changing agent context."""
        chat = self.query_one("#chat-area", VerticalScroll)
        self._reset_chat_log_with_title()
        self._last_reply_text = ""
        pending_divider = False

        for item in transcript:
            role = item.get("role", "")
            content = item.get("content", "")
            if not content.strip():
                continue
            if role == "summary":
                self._mount_compact_summary_block(content)
                self._chat_log.append(f"[上下文压缩总结]\n{content}")
                continue
            if role == "user":
                if pending_divider:
                    chat.mount(Static("─", classes="divider"))
                    pending_divider = False
                self._mount_user_message_sync(content)
                self._chat_log.append(f"你:\n{content}")
                continue
            if role == "assistant":
                visible = _sanitize_user_facing_text(content)
                self._mount_agent_message_sync(visible)
                self._chat_log.append(f"Aero:\n{visible}")
                self._last_reply_text = visible
                pending_divider = True

        if pending_divider:
            chat.mount(Static("─", classes="divider"))
        self._maybe_scroll_to_end()

    def _mount_user_message_sync(self, text: str) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        row = Horizontal(classes="message-row user-message")
        chat.mount(row)
        row.mount(
            Static(
                t("label.user", self.config.language),
                classes="message-label user-label",
            )
        )
        row.mount(Static(escape(text), classes="message-body user-content"))

    def _mount_agent_message_sync(self, text: str, *, force_scroll: bool = False) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        row = Horizontal(classes="message-row agent-message")
        chat.mount(row)
        row.mount(
            Static(
                t("label.agent", self.config.language),
                classes="message-label agent-label",
            )
        )
        stack = Vertical(classes="message-body agent-stack")
        row.mount(stack)
        stack.mount(
            ChatMarkdown(
                strip_image_markdown(text),
                classes="agent-content",
                force_scroll_on_ready=force_scroll,
            )
        )
        for path in self._resolve_inline_image_paths(text):
            try:
                index = self._register_image_attachment(path)
                stack.mount(InlineImageAttachment(path, index))
            except Exception:
                debug_log("tui.inline_image_sync_failed", path=str(path))

    def _launch_sub_agent_from_tool(
        self,
        title: str,
        task: str,
        success_criteria: str,
        context_summary: str,
    ) -> dict:
        subtask = self._subagents.create(
            title=title,
            description=task,
            success_criteria=success_criteria,
            context_summary=context_summary,
        )
        worker = self.run_worker(self._run_sub_agent_task(subtask), exclusive=False)
        self._subagent_workers[subtask.id] = worker
        self._show_subagent_handoff_notice()
        return {
            "status": "started",
            "task_id": subtask.id,
            "title": subtask.title,
            "message": "已转交后台任务。",
        }

    async def _mount_background_handoff_message(
        self,
        task_text: str,
        *,
        title: str,
        context_summary: str,
    ) -> None:
        result = self._launch_sub_agent_from_tool(
            title=title,
            task=task_text,
            success_criteria="完成用户交代的后台任务，并报告关键结果和产物路径。",
            context_summary=context_summary,
        )
        body = await self._mount_agent_message()
        message = (
            f"已经转交后台任务（#{result['task_id']}）：{result['title']}。\n\n"
            "任务完成后我会通知你，你也可以随时问我任务的进度和状态。"
        )
        await body.update(message)
        self._last_reply_text = message
        self._chat_log.append(f"Aero:\n{message}")
        chat = self.query_one("#chat-area", VerticalScroll)
        chat.mount(Static("─", classes="divider"))
        self._maybe_scroll_to_end()

    def _query_sub_agents_from_tool(self, task_id: str | None = None) -> dict:
        normalized_task_id = (task_id or "").strip() or None
        return self._subagents.snapshot(normalized_task_id)

    def _cancel_sub_agent_from_tool(self, task_id: str) -> dict:
        normalized_task_id = (task_id or "").strip()
        if not normalized_task_id:
            return {"status": "error", "message": "请提供后台任务 ID。"}
        task = self._subagents.get(normalized_task_id)
        if task is None:
            return {
                "status": "not_found",
                "message": f"未找到后台任务 #{normalized_task_id}。",
            }
        if not task.active:
            return {
                "status": "not_running",
                "message": (
                    f"后台任务 #{task.id} 当前状态为 {task.status}，无需取消。"
                ),
                "task": self._subagents.snapshot(task.id)["tasks"][0],
            }
        self._subagents.cancel(task.id)
        worker = self._subagent_workers.get(task.id)
        if worker is not None and worker.is_running:
            worker.cancel()
        self._render_footer_status()
        return {
            "status": "cancelled",
            "message": f"已取消后台任务 #{task.id}：{task.title}",
            "task": self._subagents.snapshot(task.id)["tasks"][0],
        }

    def _append_subagent_context_note(self, task: SubAgentTask) -> None:
        if self.agent is None:
            return
        note = _subagent_context_note(task)
        if _has_pending_tool_calls(self.agent.messages):
            if note not in self._pending_subagent_context_notes:
                self._pending_subagent_context_notes.append(note)
            return
        self.agent.messages.append(Message(role="assistant", content=note))

    def _flush_pending_subagent_context_notes(self) -> None:
        if self.agent is None or _has_pending_tool_calls(self.agent.messages):
            return
        while self._pending_subagent_context_notes:
            note = self._pending_subagent_context_notes.pop(0)
            self.agent.messages.append(Message(role="assistant", content=note))

    def _flush_deferred_subagent_notices(self) -> None:
        if not self._deferred_subagent_notices:
            return
        notices, self._deferred_subagent_notices = self._deferred_subagent_notices, []
        for task, message in notices:
            self.run_worker(
                self._append_subagent_notice(task, message),
                exclusive=False,
            )

    async def _run_sub_agent_task(self, task: SubAgentTask) -> None:
        from aero.agent.loop import AgentLoop

        sub_agent = AgentLoop(self.config)
        task.agent = sub_agent
        if self.agent is not None:
            sub_agent.always_allow = set(self.agent.always_allow)
            sub_agent.tracker = self.agent.tracker
        prompt = _subagent_task_prompt(task)
        response_text = ""
        try:
            async for event in sub_agent.run_stream(prompt):
                if event.type == "text":
                    response_text += event.content
                elif event.type == "status":
                    self._subagents.update(task.id, event.content)
                    self._start_subagent_footer_activity()
                elif event.type == "confirm":
                    self._subagents.pause(task.id, event.content)
                    self._start_subagent_footer_activity()
                    choice = await self._show_confirm_dialog(event.content)
                    if sub_agent.confirm_future and not sub_agent.confirm_future.done():
                        sub_agent.confirm_future.set_result(choice)
                    self._subagents.update(task.id, "已处理确认", status="running")

            artifacts = _collect_artifact_paths(response_text)
            self._subagents.finish(
                task.id,
                result_summary=response_text,
                artifacts=artifacts,
            )
            self.notify(f"后台任务完成：{task.title}", timeout=4)
            if self._agent_worker is not None and self._agent_worker.is_running:
                self._deferred_subagent_notices.append((task, _subagent_done_message(task)))
            else:
                await self._append_subagent_notice(task, _subagent_done_message(task))
            self._append_subagent_context_note(task)
            self._auto_save_session()
        except (WorkerCancelled, asyncio.CancelledError):
            self._subagents.cancel(task.id)
        except Exception as e:
            self._subagents.fail(task.id, str(e))
            self.notify(f"后台任务失败：{task.title}", severity="error", timeout=5)
            await self._append_subagent_notice(
                task,
                f"后台任务 #{task.id} 失败：{e}",
            )
        finally:
            self._subagent_workers.pop(task.id, None)
            self._render_footer_status()

    async def _append_subagent_notice(self, task: SubAgentTask, message: str) -> None:
        self._enter_chat_mode()
        body = await self._mount_agent_message()
        await body.update(message)
        self._last_reply_text = message
        self._chat_log.append(f"Aero:\n{message}")
        chat = self.query_one("#chat-area", VerticalScroll)
        chat.mount(Static("─", classes="divider"))
        await self._render_inline_images(message)
        self._maybe_scroll_to_end()

    def _handle_subagent_command(self, text: str) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        parts = text.split()
        action = parts[1] if len(parts) > 1 else "list"
        if action == "list":
            tasks = self._subagents.list()
            if not tasks:
                chat.mount(Static("[dim]当前没有后台任务。[/dim]", classes="divider"))
                return
            lines = []
            for task in tasks:
                lines.append(
                    f"#{task.id} {task.status} · {task.title} · {task.latest_status}"
                )
            chat.mount(Static("\n".join(lines), classes="divider"))
            return

        if len(parts) < 3:
            chat.mount(
                Static(
                    "[dim]用法：/subagent list|cancel|allow|deny <id>[/dim]",
                    classes="divider",
                )
            )
            return

        task_id = parts[2]
        task = self._subagents.get(task_id)
        if task is None:
            chat.mount(Static(f"[dim]未找到后台任务 #{task_id}。[/dim]", classes="divider"))
            return

        if action == "cancel":
            result = self._cancel_sub_agent_from_tool(task_id)
            chat.mount(Static(f"[dim]{result['message']}[/dim]", classes="divider"))
            return

        if action in {"allow", "deny"}:
            future = getattr(task.agent, "confirm_future", None)
            if future is None or future.done():
                chat.mount(Static(f"[dim]后台任务 #{task_id} 当前不需要确认。[/dim]", classes="divider"))
                return
            future.set_result("allow" if action == "allow" else "deny")
            task.status = "running" if action == "allow" else "cancelled"
            task.latest_status = "已允许继续" if action == "allow" else "已拒绝继续"
            self._render_footer_status()
            chat.mount(Static(f"[dim]已处理后台任务 #{task_id}。[/dim]", classes="divider"))
            return

        chat.mount(Static("[dim]用法：/subagent list|cancel|allow|deny <id>[/dim]", classes="divider"))

    async def _handle_compact_command(self, text: str) -> None:
        lang = self.config.language
        chat = self.query_one("#chat-area", VerticalScroll)
        if self.agent is None or len(self.agent.messages) <= 2:
            chat.mount(Static(
                f"[dim]{t('app.compact_nothing', lang)}[/dim]",
                classes="divider",
            ))
            return

        messages = self.agent.messages
        system_msg = messages[0]
        if len(messages) <= 3:
            chat.mount(Static(
                f"[dim]{t('app.compact_short', lang)}[/dim]",
                classes="divider",
            ))
            return

        before_tokens = _estimate_context_tokens(messages)

        summary_prompt = (
            "Summarize the full conversation below so future turns can continue "
            "from the summary alone. Remove unimportant chatter and redundant detail. "
            "Keep user goals, decisions, constraints, current task state, completed work, "
            "tool results, file paths, URLs, data names, numerical values, errors, and any "
            "preferences the user expressed. Be concise but complete.\n\n"
        )
        for msg in messages[1:]:
            role = msg.role
            content = msg.content[:2000]
            if msg.tool_calls:
                names = [tc.name for tc in msg.tool_calls]
                content += f"\n[tool_calls: {', '.join(names)}]"
            summary_prompt += f"[{role}]: {content}\n\n"
        summary_prompt += "\nNow provide the compacted context summary only."

        loading = Static("", classes="divider")
        chat.mount(loading)
        self._scroll_chat_to_end()

        async def animate_loading() -> None:
            frame = 0
            while True:
                loading.update(
                    f"[#8bdcff]{_activity_indicator(frame)} {t('app.compact_running', lang)}[/#8bdcff]"
                )
                self._scroll_chat_to_end()
                frame += 1
                await asyncio.sleep(0.12)

        loading_task = asyncio.create_task(animate_loading())

        async def stop_loading() -> None:
            loading_task.cancel()
            with suppress(asyncio.CancelledError):
                await loading_task

        try:
            from aero.agent.llm_client import LLMConfig, LLMClient
            llm_cfg = LLMConfig(
                provider=self.config.llm.provider,
                model=self.config.llm.model,
                api_key=self.config.llm.active_api_key(),
                base_url=self.config.llm.base_url,
            )
            client = LLMClient(llm_cfg)
            summary_lines = await client.chat([
                Message(role="user", content=summary_prompt)
            ])
            await client.close()
        except Exception as e:
            await stop_loading()
            loading.update(
                f"[dim]{t('app.compact_error', lang).format(error=str(e))}[/dim]",
            )
            self._scroll_chat_to_end()
            return

        summary_text = summary_lines.strip()
        if not summary_text:
            await stop_loading()
            loading.update(
                f"[dim]{t('app.compact_error', lang).format(error='empty summary')}[/dim]",
            )
            self._scroll_chat_to_end()
            return

        compacted = _compacted_context_messages(system_msg, summary_text)
        self.agent.messages = compacted
        after_tokens = _estimate_context_tokens(compacted)
        self.agent.tracker.current_prompt_tokens = after_tokens
        ratio = (1 - after_tokens / max(before_tokens, 1)) * 100

        await stop_loading()
        loading.update(
            f"[#8bdcff]{t('app.compact_done', lang).format(before=format_token_count(before_tokens), after=format_token_count(after_tokens), ratio=ratio)}[/#8bdcff]",
        )
        self._mount_compact_summary_block(summary_text)
        self._scroll_chat_to_end()
        self._refresh_model_info()
        self.query_one("#user-input", TextArea).focus()

    async def _handle_instructions_command(self, text: str) -> None:
        from aero.data.instructions import load_instructions, clear_instructions, GLOBAL_INSTRUCTIONS_PATH

        lang = self.config.language
        chat = self.query_one("#chat-area", VerticalScroll)
        project_dir = self.config.output.data_dir

        if text.strip() == "/instructions clear":
            clear_instructions(scope="project", project_dir=project_dir)
            chat.mount(Static(
                f"[dim]{t('app.instructions_cleared', lang)}[/dim]",
                classes="divider",
            ))
            return

        instructions = load_instructions(project_dir=project_dir)
        if not instructions:
            lines = [
                f"[dim]{t('app.instructions_empty', lang)}[/dim]\n",
                "[dim]",
                t('app.instructions_hint', lang),
                "[/dim]",
            ]
            chat.mount(Static("".join(lines), classes="divider"))
            return

        chat.mount(Static(
            f"[bold]{t('app.instructions_title', lang)}[/bold]\n\n{instructions}",
            classes="divider",
        ))

    def _set_model(self, value: str) -> None:
        lang = self.config.language
        chat = self.query_one("#chat-area", VerticalScroll)
        model = _resolve_model_alias(self.config.llm.provider, value)
        if not model:
            chat.mount(Static(t("error.model_empty", lang)))
            return
        self.config.llm.model = model
        self.config.llm.apply_active_provider_defaults()
        self._sync_agent_llm_config()
        if self.persist_config:
            save_llm_profile(
                self.config.llm.provider,
                self.config.llm.active_api_key(),
                self.config.llm.model,
                self.config.llm.base_url,
            )
            _save_config(self.config)
        self._refresh_model_info()
        self._set_footer_status(t("app.model_switched", lang).format(
            provider=self.config.llm.provider, model=model))

    def _set_provider(self, value: str) -> None:
        lang = self.config.language
        previous_provider = self.config.llm.provider
        provider = normalize_provider_id(value)
        if provider == "official" and not OFFICIAL_ACCOUNT_UI_ENABLED:
            self.query_one("#chat-area", VerticalScroll).mount(
                Static("[error]当前版本暂未开放 Aerolytica 官方模型。[/error]")
            )
            return
        preset = get_provider_preset(provider)
        chat = self.query_one("#chat-area", VerticalScroll)
        if preset is None and provider != "custom":
            chat.mount(Static(f"[error]未知模型服务商: {value}[/error]"))
            return

        self.config.llm.apply_active_provider_defaults()
        self.config.llm.switch_provider(provider)
        if preset is not None:
            provider_config = self.config.llm.provider_config(provider)
            if not provider_config.base_url:
                provider_config.base_url = preset.base_url
            if not provider_config.model:
                provider_config.model = preset.default_model
            self.config.llm.use_provider_settings()
        self._sync_agent_llm_config()
        if provider != "official":
            self._official_available_credits = None
        if self.persist_config:
            _save_config(self.config)
        self._refresh_model_info()
        status = t("app.provider_switched", lang).format(
            provider=_display_provider_name(provider),
            model=self.config.llm.model,
        )
        missing_api_key = (
            previous_provider != provider
            and not self.config.llm.active_api_key()
            and provider != "official"
        )
        from aero.core.official_account import load_official_session

        missing_official_login = provider == "official" and not load_official_session().is_logged_in
        if missing_api_key:
            status += "，请完成 API Key 配置"
        elif missing_official_login:
            status += "，请登录官方账户"
        self._set_footer_status(status)
        if missing_api_key:
            self.call_after_refresh(self._open_provider_setup, previous_provider)
        elif missing_official_login:
            self.run_worker(self._handle_official_login(), exclusive=True)
        elif provider == "official":
            self.run_worker(self._refresh_official_credits(), exclusive=False)

    def _set_reasoning_effort(self, value: str) -> None:
        lang = self.config.language
        chat = self.query_one("#chat-area", VerticalScroll)
        effort = _normalize_reasoning_effort(value)
        if effort is None:
            chat.mount(Static(t("error.effort_options", lang)))
            return
        self.config.llm.reasoning_effort = effort
        if self.agent is not None:
            self.agent.llm.config.reasoning_effort = effort
        if self.persist_config:
            _save_config(self.config)
        self._refresh_model_info()
        self._set_footer_status(t("app.effort_set", lang).format(effort=effort or "auto"))

    def _set_theme(self, value: str) -> None:
        lang = self.config.language
        chat = self.query_one("#chat-area", VerticalScroll)
        theme = _resolve_theme_name(value, self.available_themes)
        if theme is None:
            options = ", ".join(self.available_themes)
            chat.mount(Static(t("error.theme_unknown", lang).format(options=options)))
            return
        self.theme = theme
        _save_user_theme(theme)
        self._set_footer_status(t("app.theme_switched", lang).format(theme=theme))

    def _preview_theme(self, value: str) -> None:
        theme = _resolve_theme_name(value, self.available_themes)
        if theme is not None:
            self.theme = theme

    def _refresh_model_info(self) -> None:
        if self._model_info is not None:
            self._model_info.update(self._model_status_text())
        if self._input_meta is not None:
            self._input_meta.update(self._input_meta_text())

    def _set_footer_status(self, message: str, timeout: float = 4.0) -> None:
        if self._footer_status is None:
            return
        self._footer_status_token += 1
        token = self._footer_status_token
        self._footer_temp_text = message
        self._render_footer_status()

        def clear_if_current() -> None:
            if self._footer_status is not None and token == self._footer_status_token:
                self._footer_temp_text = ""
                self._render_footer_status()

        self.set_timer(timeout, clear_if_current)

    def _show_subagent_handoff_notice(self) -> None:
        self._subagent_notice_until = time.monotonic() + 2.0
        self._render_footer_status()
        self._start_subagent_footer_activity()

    def _start_subagent_footer_activity(self) -> None:
        if self._subagent_footer_active:
            return
        self._subagent_footer_active = True
        self._tick_subagent_footer()

    def _tick_subagent_footer(self) -> None:
        active = bool(self._subagents.active())
        notice_active = time.monotonic() < self._subagent_notice_until
        if not active and not notice_active:
            self._subagent_footer_active = False
            self._render_footer_status()
            return
        self._subagent_footer_frame += 1
        self._render_footer_status()
        self.set_timer(0.18, self._tick_subagent_footer)

    def _render_footer_status(self) -> None:
        if self._footer_status is None:
            return
        if time.monotonic() < self._subagent_notice_until:
            self._footer_status.update("[dim]已转交后台任务[/dim]")
            return
        subagent_text = self._subagents.footer_text(self._subagent_footer_frame)
        if subagent_text:
            self._footer_status.update(f"[dim]{escape(subagent_text)}[/dim]")
            return
        if self._footer_temp_text:
            self._footer_status.update(f"[dim]{escape(self._footer_temp_text)}[/dim]")
            return
        self._footer_status.update("")

    def _start_activity(self) -> None:
        self._activity_running = True
        self._activity_frame = 0
        self._render_activity()
        self.set_timer(0.12, self._tick_activity)

    def _tick_activity(self) -> None:
        if not self._activity_running:
            return
        worker_running = self._agent_worker is not None and self._agent_worker.is_running
        if not worker_running:
            self._stop_activity()
            return
        self._activity_frame += 1
        self._render_activity()
        self.set_timer(0.12, self._tick_activity)

    def _render_activity(self) -> None:
        panel = self._status_msg
        if panel is None or panel.collapsed or panel.done:
            return
        self._render_status_expanded(panel, limit=8)

    def _stop_activity(self) -> None:
        self._activity_running = False
        panel = self._status_msg
        if panel is not None and not panel.collapsed:
            self._render_status_expanded(panel, limit=None if panel.user_expanded else 8)

    def _handle_revoke(self, text: str) -> None:
        lang = self.config.language
        chat = self.query_one("#chat-area", VerticalScroll)
        if self.agent is None:
            chat.mount(Static(t("error.agent_init", lang).format(error=self.last_error)))
            return

        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            if not self.agent.always_allow:
                chat.mount(Static(t("info.always_allow_none", lang)))
            else:
                tools = ", ".join(sorted(self.agent.always_allow))
                chat.mount(Static(t("info.always_allow_list", lang).format(tools=tools)))
                chat.mount(Static(t("info.always_allow_hint", lang)))
            return

        tool_name = parts[1].strip()
        if tool_name in self.agent.always_allow:
            self.agent.always_allow.discard(tool_name)
            chat.mount(Static(t("info.always_allow_revoked", lang).format(tool=tool_name)))
        else:
            chat.mount(Static(t("error.revoke_not_found", lang).format(tool=tool_name)))

    def _handle_help(self) -> None:
        self.push_screen(HelpScreen(self.config.language))
        self.query_one("#user-input", TextArea).focus()

    def _select_command(self) -> None:
        cmd_list = self._cmd_list
        if cmd_list.index is None or len(self._filtered_commands) == 0:
            return
        cmd, _ = self._filtered_commands[cmd_list.index]
        inp = self.query_one("#user-input", TextArea)
        if cmd.endswith(" ") or _command_requires_input(cmd):
            inp.load_text(cmd if cmd.endswith(" ") else cmd + " ")
        elif cmd == "/model":
            inp.load_text("/model ")
        elif cmd == "/variants":
            inp.load_text("/variants ")
        elif cmd == "/language":
            inp.load_text("/language ")
        elif cmd == "/theme":
            inp.load_text("/theme ")
        elif cmd == "/preview":
            inp.load_text("/preview ")
        else:
            inp.load_text(cmd)
        inp.focus()
        self._hide_command_list()

    def _execute_selected_command(self) -> None:
        cmd_list = self._cmd_list
        if cmd_list is None or cmd_list.index is None or len(self._filtered_commands) == 0:
            return
        index = max(0, min(cmd_list.index, len(self._filtered_commands) - 1))
        cmd, _ = self._filtered_commands[index]
        inp = self.query_one("#user-input", TextArea)
        if cmd.endswith(" ") or _command_requires_input(cmd):
            inp.load_text(cmd if cmd.endswith(" ") else cmd + " ")
            inp.action_cursor_line_end()
            inp.focus()
            self._hide_command_list()
            return
        inp.clear()
        self._hide_command_list(focus_input=False)
        self.run_worker(self._process(cmd), exclusive=False)

    def _hide_command_list(self, focus_input: bool = True) -> None:
        if self._cmd_list is None:
            return
        for item in self._cmd_list.children:
            if isinstance(item, ListItem):
                item.set_class(False, "command-selected")
        self._cmd_list.styles.display = "none"
        self.query_one("#command-row").styles.display = "none"
        if self.screen.has_class("startup"):
            self.screen.refresh(repaint=True, layout=True)
        if focus_input:
            self.query_one("#user-input", TextArea).focus()

    @work(exclusive=False)
    async def _run_agent(self, text: str, state: AgentRunState) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        lang = self.config.language
        debug_log("tui.agent_worker_started", text_length=len(text))
        agent_msg = state.agent_msg

        # Ensure session_id is assigned before the agent runs, so that any
        # tool calls during the response (e.g. write_plan_document) are saved
        # under the correct plans/<session_id>/ subdirectory.
        if self._session_id is None:
            import uuid
            self._session_id = uuid.uuid4().hex[:12]
        set_session_id(self._session_id)
        experiment = self._get_experiment_mgr().active()
        workspace = (
            self._get_experiment_mgr().workspace_path(experiment)
            if experiment is not None
            else self._project_dir
        )

        async def safe_update(message: str) -> None:
            try:
                await agent_msg.update(message)
            except Exception as e:
                debug_log(
                    "tui.agent_markdown_update_failed",
                    error=repr(e),
                    traceback=traceback.format_exc(),
                    message_length=len(message),
                )
                fallback = "\n".join(f"    {line}" for line in message.splitlines()) or "    "
                await agent_msg.update(fallback)

        _THINKING_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
        self._thinking_frame = 0
        thinking_active = True
        thought = ""

        def _tick_thinking() -> None:
            if not thinking_active or state.background_task is not None:
                return
            spinner = _THINKING_SPINNER[self._thinking_frame % len(_THINKING_SPINNER)]
            self._thinking_frame += 1
            nonlocal thought
            suffix = ""
            if thought:
                suffix = f"  *{escape(thought)}*"
            agent_msg.update(
                f"*{spinner} {t('app.thinking', lang)}{suffix}*"
            )
            self._maybe_scroll_to_end()
            if thinking_active:
                self.set_timer(0.15, _tick_thinking)

        _tick_thinking()

        try:
            response_text = ""
            content_blocked = False
            status_auto_collapsed = False
            vision_setup_requested = False

            def maybe_handoff_claimed_background(status_text: str) -> bool:
                if state.background_task is not None:
                    return False
                if _is_subagent_tool_status(status_text):
                    return False
                if not _assistant_claims_background_handoff(response_text):
                    return False
                self._handoff_main_run_to_background()
                return state.background_task is not None

            with (
                use_workspace(self._project_dir, workspace),
                use_subagent_launcher(self._launch_sub_agent_from_tool),
                use_subagent_status_provider(self._query_sub_agents_from_tool),
                use_subagent_canceller(self._cancel_sub_agent_from_tool),
                use_secret_input_provider(
                    self._request_secret_input, self._take_secret_handle
                ),
                use_checkpoint_creator(
                    lambda name: self._create_checkpoint(name=name, kind="agent")
                ),
            ):
                async for event in state.agent.run_stream(text):
                    debug_log(
                        "tui.agent_event",
                        event_type=event.type,
                        content_length=len(event.content or ""),
                    )
                    if event.type == "text":
                        state.phase = "text"
                        thinking_active = False
                        self._streaming_text = True
                        response_text += event.content
                        if state.background_task is not None:
                            continue
                        if (
                            self._status_msg is not None
                            and not status_auto_collapsed
                            and not self._status_msg.user_expanded
                        ):
                            self._collapse_status(done=True)
                            status_auto_collapsed = True
                        await safe_update(
                            _sanitize_user_facing_text(response_text)
                        )
                        self._maybe_scroll_to_end()
                    elif event.type == "status":
                        state.phase = "tool"
                        credential_scope = _credential_setup_scope(event.content)
                        if credential_scope is not None:
                            self._pending_credential_scope = credential_scope
                        if (
                            not vision_setup_requested
                            and _vision_setup_required(event.content)
                        ):
                            vision_setup_requested = True
                            await self._open_vision_setup_for_current_task()
                        maybe_handoff_claimed_background(event.content)
                        if state.background_task is not None:
                            self._subagents.update(state.background_task.id, event.content)
                            self._start_subagent_footer_activity()
                            continue
                        if thinking_active:
                            thought = escape(event.content)[:50]
                        if self._append_status(event.content):
                            self._maybe_scroll_to_end()
                    elif event.type == "confirm":
                        state.phase = "tool"
                        maybe_handoff_claimed_background(event.content)
                        if state.background_task is not None:
                            self._subagents.pause(state.background_task.id, event.content)
                            self._start_subagent_footer_activity()
                            choice = await self._show_confirm_dialog(event.content)
                            if (
                                state.background_task.agent is not None
                                and state.background_task.agent.confirm_future is not None
                                and not state.background_task.agent.confirm_future.done()
                            ):
                                state.background_task.agent.confirm_future.set_result(choice)
                            self._subagents.update(state.background_task.id, "已处理确认", status="running")
                        else:
                            await self._handle_confirm(event.content)
                    elif event.type == "content_blocked":
                        thinking_active = False
                        content_blocked = True
                        if state.background_task is not None:
                            self._subagents.fail(state.background_task.id, event.content)
                            response_text = ""
                            continue
                        self._strike_last_user_message()
                        await safe_update(
                            f"**内容被拦截**\n\n{event.content}"
                        )
                        self._maybe_scroll_to_end()
                        response_text = ""

            if state.background_task is not None:
                task = state.background_task
                artifacts = _collect_artifact_paths(response_text)
                self._subagents.finish(task.id, result_summary=response_text, artifacts=artifacts)
                self.notify(f"后台任务完成：{task.title}", timeout=4)
                await self._append_subagent_notice(task, _subagent_done_message(task))
                self._append_subagent_context_note(task)
                self._auto_save_session()
                self._maybe_scroll_to_end()
                debug_log("tui.agent_worker_background_completed", task_id=task.id)
                return

            if not response_text and not content_blocked:
                thinking_active = False
                await safe_update("")
                self._collapse_status(done=True)
            elif content_blocked:
                self._collapse_status(done=True)
                chat.mount(Static("─", classes="divider"))
            else:
                sanitized_response = _sanitize_user_facing_text(response_text)
                self._last_reply_text = sanitized_response
                self._chat_log.append(f"Aero:\n{sanitized_response}")
                self._collapse_status(done=True)
                chat.mount(Static("─", classes="divider"))
                await self._render_inline_images(sanitized_response)
            self._maybe_scroll_to_end()
            debug_log("tui.agent_worker_completed", response_length=len(response_text))

        except (WorkerCancelled, asyncio.CancelledError):
            thinking_active = False
            self._streaming_text = False
            debug_log("tui.agent_worker_cancelled")
            if state.background_task is not None:
                self._subagents.cancel(state.background_task.id)
            else:
                await safe_update("*已中断。*")
                self._chat_log.append("Aero:\n已中断。")
                self._collapse_status(done=True)
                chat.mount(Static("─", classes="divider"))
                self._maybe_scroll_to_end()

        except Exception as e:
            thinking_active = False
            self._streaming_text = False
            debug_log(
                "tui.agent_worker_error",
                error=repr(e),
                traceback=traceback.format_exc(),
            )
            if state.background_task is not None:
                self._subagents.fail(state.background_task.id, str(e))
                await self._append_subagent_notice(
                    state.background_task,
                    f"后台任务 #{state.background_task.id} 失败：{e}",
                )
                return
            if _is_llm_auth_error(e):
                self.config.llm.set_active_api_key("")
                if getattr(self, "persist_config", False):
                    _save_config(self.config)
                self._refresh_agent_llm_config()
                message = _llm_auth_setup_message(self.config)
                await safe_update(message)
                self._last_reply_text = message
                self._chat_log.append(f"Aero:\n{message}")
                chat.mount(Static("─", classes="divider"))
                self._maybe_scroll_to_end()
                return
            if "Content Exists Risk" in str(e) or "content_filter" in str(e).lower():
                await safe_update(
                    "**内容被拦截**\n\n当前消息触发了模型服务商的内容安全策略，"
                    "本轮对话已被排除在上下文之外，不会影响后续对话。\n"
                    "请换一种表述方式重试，或用 /provider 切换到其他服务商。"
                )
                self._last_reply_text = ""
                self._strike_last_user_message()
                chat.mount(Static("─", classes="divider"))
                self._maybe_scroll_to_end()
                return
            await safe_update(f"**错误:** {e}")
            self._chat_log.append(f"Aero:\n错误: {e}")

        finally:
            if state.background_task is not None:
                self._subagent_workers.pop(state.background_task.id, None)
                self._render_footer_status()
            if self._main_run_state is state:
                self._flush_pending_subagent_context_notes()
                self._flush_deferred_subagent_notices()
                self._agent_worker = None
                self._main_run_state = None
                self._stop_activity()
                self.sub_title = self._ready_subtitle()
                if self.config.llm.provider == "official":
                    await self._refresh_official_credits()
                self._refresh_model_info()
                self._auto_save_session()
                self._streaming_text = False
                self._flush_queued_message()
                self.query_one("#user-input", TextArea).focus()

    def _flush_queued_message(self) -> None:
        msg = self._queued_message
        self._queued_message = None
        if msg:
            self.run_worker(self._process(msg), exclusive=False)

    def action_copy_last_reply(self) -> None:
        if not self._last_reply_text.strip():
            self.notify("暂无可复制的回复", severity="warning", timeout=2)
            return
        try:
            self._copy_text_to_clipboard(self._last_reply_text)
        except Exception as exc:
            self.notify(f"复制失败: {exc}", severity="error", timeout=3)
            return
        self.notify("已复制最后一条回复", timeout=2)

    def _copy_text_to_clipboard(self, text: str) -> None:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text, text=True, check=True)
            return
        self.copy_to_clipboard(text)

    def _append_status(self, message: str) -> bool:
        chat = self.query_one("#chat-area", VerticalScroll)
        if self._status_msg is None:
            self._status_msg = StatusPanel(
                len(self._status_sessions) + 1,
                classes="status-panel",
            )
            self._status_sessions.append(self._status_msg)
            chat.mount(self._status_msg)
            debug_log("tui.status_panel_created", panel_index=self._status_msg.index)

        text = message.strip()
        if not text:
            return False
        self._status_msg.collapsed = False
        self._status_msg.done = False
        self._status_msg.user_expanded = False
        should_scroll = self._upsert_status_line(self._status_msg, text)
        debug_log(
            "tui.status_appended",
            panel_index=self._status_msg.index,
            lines=len(self._status_msg.lines),
            message_length=len(text),
            progress_slot=_status_progress_slot(text),
            should_scroll=should_scroll,
        )
        self._render_status_expanded(self._status_msg, limit=8)
        return should_scroll

    def _upsert_status_line(self, panel: StatusPanel, text: str) -> bool:
        if _is_replaceable_status(text):
            if _is_completed_download_progress(text):
                panel.lines[:] = [
                    line for line in panel.lines if not _is_same_status_slot(line, text)
                ]
                return False
            for index in range(len(panel.lines) - 1, -1, -1):
                if _is_same_status_slot(panel.lines[index], text):
                    panel.lines.pop(index)
                    panel.lines.append(text)
                    return index != len(panel.lines) - 1
            panel.lines.append(text)
            return True

        insert_at = next(
            (
                index
                for index, line in enumerate(panel.lines)
                if _is_replaceable_status(line)
            ),
            len(panel.lines),
        )
        panel.lines.insert(insert_at, text)
        return True

    def _collapse_status(self, done: bool = False) -> None:
        panel = self._status_msg
        if panel is None or not panel.lines:
            return
        panel.done = done
        panel.collapsed = True
        debug_log(
            "tui.status_collapse_requested",
            panel_index=panel.index,
            done=done,
            lines=len(panel.lines),
        )
        self._render_status_collapsed(panel)

    def _toggle_status_panel(self, panel: StatusPanel) -> None:
        if not panel.lines:
            return
        panel.user_expanded = panel.collapsed
        if panel.collapsed:
            self._render_status_expanded(panel, limit=None)
        else:
            self._render_status_collapsed(panel)
        debug_log(
            "tui.status_toggled",
            panel_index=panel.index,
            collapsed=panel.collapsed,
            user_expanded=panel.user_expanded,
        )

    def _render_status_expanded(self, panel: StatusPanel, limit: int | None = None) -> None:
        if not panel.lines:
            return
        panel.set_class(False, "status-collapsed")
        panel.set_class(True, "status-expanded")
        lang = self.config.language
        lines = panel.lines if limit is None else panel.lines[-limit:]
        rendered = _render_status_lines(
            lines,
            activity=_activity_indicator(self._activity_frame)
            if self._activity_running and not panel.done
            else None,
        )
        panel.update(f"- {t('log.panel_title_plain', lang)}\n{rendered}")
        panel.set_collapsed(False)
        debug_log(
            "tui.status_rendered_expanded",
            panel_index=panel.index,
            lines=len(lines),
            rendered_length=len(rendered),
            limited=limit is not None,
        )

    def _render_status_collapsed(self, panel: StatusPanel) -> None:
        if not panel.lines:
            return
        panel.set_class(True, "status-collapsed")
        panel.set_class(False, "status-expanded")
        lang = self.config.language
        total = len(panel.lines)
        if panel.done:
            text = t("log.collapsed_done", lang).format(total=total)
        else:
            last_line = _display_status_line(panel.lines[-1])
            text = t("log.collapsed_last", lang).format(
                total=total,
                last_line=last_line,
            )
        panel.update(f"+ {text}")
        panel.set_collapsed(True)
        debug_log(
            "tui.status_rendered_collapsed",
            panel_index=panel.index,
            done=panel.done,
            total=total,
        )

    async def _show_confirm_dialog(self, content: str) -> str:
        data = json.loads(content)
        tool_name = data.get("tool", "")
        args = data.get("args", {})
        batch_args = data.get("batch_args", None)
        self.screen.add_class("confirming")
        try:
            if tool_name == "propose_execution":
                if self.config.mode != "plan":
                    return "approve"
                if self._status_msg is not None:
                    self._collapse_status(done=False)
                screen = ExecutionApprovalScreen(self.config.language)
                return await self.push_screen_wait(screen)
            message = self._build_confirm_message(tool_name, args, batch_args)
            if tool_name in {
                "record_memo",
                "update_memo",
                "delete_memo",
                "clear_memos",
                "restore_paper_version",
            }:
                labels = {
                    "record_memo": ("加入备忘录", "确认加入"),
                    "update_memo": ("更新备忘录", "确认更新"),
                    "delete_memo": ("删除备忘录", "确认删除"),
                    "clear_memos": ("清理备忘录", "确认清理"),
                    "restore_paper_version": ("恢复论文版本", "恢复到此版"),
                }
                title, allow_label = labels[tool_name]
                screen = ConfirmScreen(
                    message,
                    self.config.language,
                    title=title,
                    allow_label=allow_label,
                    deny_label="取消",
                    show_always=False,
                )
                return await self.push_screen_wait(screen)
            screen = ConfirmScreen(message, self.config.language)
            return await self.push_screen_wait(screen)
        finally:
            self.screen.remove_class("confirming")

    async def _request_secret_input(self, request: SecretInputRequest) -> dict:
        value = await self.push_screen_wait(SecretInputScreen(request))
        if value is None:
            return {"status": "cancelled", "scope": request.scope}
        handle = secrets.token_urlsafe(24)
        self._secret_handles[handle] = value
        return {
            "status": "submitted",
            "scope": request.scope,
            "secret_handle": handle,
            "message": "已收到本地凭据；请用 secret_handle 调用对应保存工具。",
        }

    def _take_secret_handle(self, handle: str) -> str | None:
        return self._secret_handles.pop(handle, None)

    async def _handle_confirm(self, content: str) -> None:
        tool_name = str(json.loads(content).get("tool", ""))
        choice = await self._show_confirm_dialog(content)
        debug_log("tui.confirm_dialog_closed", choice=choice)
        choice = _normalize_confirm_choice(choice)
        if choice in {"approve", "allow", "always"}:
            if self.config.mode == "plan" and tool_name == "propose_execution":
                debug_log("tui.confirm_mode_switch", from_mode="plan", to_mode="execute")
                self._set_mode("execute")
        elif choice == "deny":
            pass
        has_future = (
            self.agent is not None
            and self.agent.confirm_future is not None
            and not self.agent.confirm_future.done()
        )
        debug_log(
            "tui.confirm_setting_future",
            final_choice=choice,
            has_agent=self.agent is not None,
            has_future=self.agent is not None and self.agent.confirm_future is not None if self.agent else False,
        )
        if has_future:
            self.agent.confirm_future.set_result(choice)

    def _build_confirm_message(
        self,
        tool_name: str,
        args: dict,
        batch_args: list | None = None,
    ) -> str:
        lang = self.config.language
        action_label = _CONFIRM_ACTION_LABELS.get(tool_name, "执行操作")
        if tool_name == "record_memo":
            proposals = batch_args or [args]
            blocks = []
            for index, proposal in enumerate(proposals, start=1):
                title = str(proposal.get("title") or "未命名结论").strip()
                content = str(proposal.get("content") or "").strip()
                evidence = str(proposal.get("evidence") or "").strip()
                block = f"{index}. {title}\n\n{content}"
                if evidence:
                    block += f"\n\n依据或限制：{evidence}"
                blocks.append(block)
            return "准备加入以下研究备忘：\n\n" + "\n\n──\n\n".join(blocks)
        if tool_name == "update_memo":
            fields = []
            for label, key in (
                ("标题", "title"),
                ("正文", "content"),
                ("依据或限制", "evidence"),
            ):
                value = args.get(key)
                if value is not None:
                    fields.append(f"{label}：{value}")
            if args.get("tags") is not None:
                fields.append(f"标签：{'、'.join(args.get('tags') or []) or '无'}")
            changes = "\n\n".join(fields) or "未提供更新内容"
            return f"准备更新备忘录 {args.get('memo_id', '未知')}：\n\n{changes}"
        if tool_name == "delete_memo":
            return f"将永久删除备忘录：\n\n  {args.get('memo_id', '未知')}"
        if tool_name == "clear_memos":
            return "将永久清理当前项目的全部研究备忘录。此操作不可撤销。"
        if tool_name == "restore_paper_version":
            return (
                f"将论文正文恢复到版本：\n\n  {args.get('version_id', '未知')}\n\n"
                "只会覆盖当前项目绑定的 Markdown 论文正文，其他文件不会改变。"
                "若正文有未保存变化，恢复前会自动保存一个保护版本。"
            )
        if batch_args:
            if tool_name == "delete_file":
                paths = [a.get("file_path", "未知") for a in batch_args]
                visible_paths = paths[:6]
                files_str = "\n".join(
                    f"  {index}. {_truncate_middle(str(path), 72)}"
                    for index, path in enumerate(visible_paths, start=1)
                )
                if len(paths) > len(visible_paths):
                    files_str += f"\n  ... 其余 {len(paths) - len(visible_paths)} 个文件"
                return (
                    f"← {t('confirm.batch_delete_title', lang)}\n\n"
                    f"{t('confirm.files', lang)} ({len(paths)})\n\n"
                    f"{files_str}\n\n"
                    f"{t('confirm.irreversible_short', lang)}"
                )
            if tool_name == "run_shell":
                commands = [str(item.get("command") or "") for item in batch_args]
                try:
                    from aero.toolbox.tools.runtime import _redact_shell_command

                    commands = [_redact_shell_command(command) for command in commands]
                except Exception:
                    pass
                command_list = "\n\n".join(
                    f"{index}.\n  {command or '（空命令）'}"
                    for index, command in enumerate(commands, 1)
                )
                workdirs = []
                for item in batch_args:
                    workdir = str(item.get("workdir") or ".")
                    if workdir not in workdirs:
                        workdirs.append(workdir)
                runtime_setup = bool(commands) and all(
                    ".aero/runtime" in command.replace("\\", "/")
                    and re.match(r"\s*(?:mkdir\s+-p|ln\s+-[sfn ]+)", command)
                    for command in commands
                )
                if runtime_setup:
                    return (
                        "← 初始化 Aero 私有运行环境\n\n"
                        "为运行绘图和数据处理，Aero 需要准备自己的运行时目录。\n\n"
                        f"命令（{len(commands)} 条）\n\n{command_list}\n\n"
                        "影响说明\n"
                        "  仅影响 Aero 私有运行环境，不会修改当前项目文件\n"
                        "  不会删除数据；如不再需要，可用 `aero runtime clean` 清理\n\n"
                        "是否允许？"
                    )
                workdir_text = "、".join(workdirs)
                return (
                    f"← 执行 Shell 命令\n\n"
                    f"工作目录：{workdir_text}\n\n"
                    f"命令（{len(commands)} 条）\n\n{command_list}"
                )
            args_summary = "\n".join(
                _truncate_middle(json.dumps(a, ensure_ascii=False, default=str), 96)
                for a in batch_args[:8]
            )
            if len(batch_args) > 8:
                args_summary += f"\n... 其余 {len(batch_args) - 8} 次调用"
            return (
                f"← {action_label}\n\n"
                f"{t('confirm.calls', lang)} ({len(batch_args)})\n\n"
                f"{t('confirm.arguments_label', lang)}\n{args_summary}\n\n"
                f"{t('confirm.irreversible_short', lang)}"
            )

        if tool_name == "delete_file":
            file_path = args.get("file_path", "未知")
            return (
                f"← {t('confirm.delete_file_title', lang)}\n\n"
                f"{t('confirm.path', lang)}\n\n"
                f"  {_truncate_middle(str(file_path), 72)}\n\n"
                f"{t('confirm.irreversible_short', lang)}"
            )
        if tool_name == "run_shell":
            desc = args.get("description", "")
            cmd = args.get("command", "")
            wd = args.get("workdir", ".")
            cmd_display = _truncate_middle(str(cmd), 72)
            return (
                f"← {t('confirm.shell_command', lang)}\n\n"
                f"{t('confirm.description', lang)}\n  {desc}\n"
                f"{t('confirm.workdir', lang)}\n  {wd}\n"
                f"{t('confirm.command', lang)}\n  {cmd_display}"
            )
        if tool_name == "ensure_runtime_tools":
            tools = _format_tool_list(args.get("tools"))
            if lang == "en":
                return (
                    "← Install command-line tools\n\n"
                    "Aero needs these local commands to continue:\n"
                    f"  {tools}\n\n"
                    "What will change\n"
                    "  Create or update the conda environment: aero-agent\n"
                    "  Install mamba inside aero-agent if it is missing, then use it for speed\n"
                    "  Install packages from conda-forge\n"
                    "  Link the commands into your conda bin so future tasks can use them\n\n"
                    "This changes your local conda environment."
                )
            return (
                "← 安装命令行工具\n\n"
                "Aero 需要先补齐这些本机命令，才能继续处理数据：\n"
                f"  {tools}\n\n"
                "将会做什么\n"
                "  创建或更新 conda 环境：aero-agent\n"
                "  如果缺少 mamba，会把 mamba 安装到 aero-agent 内并用它加速依赖解析\n"
                "  从 conda-forge 安装对应软件包\n"
                "  把命令软链接到 conda 的 bin 目录，后续任务可直接使用\n\n"
                "这会修改本机 conda 环境，因此需要你确认。"
            )
        args_summary = json.dumps(args, ensure_ascii=False, indent=2, default=str)
        if len(args_summary) > 500:
            args_summary = args_summary[:500] + "\n..."
        return (
            f"← {action_label}\n\n"
            f"{t('confirm.arguments_label', lang)}\n{args_summary}\n\n"
            f"{t('confirm.irreversible_short', lang)}"
        )

    def _init_agent(self) -> None:
        try:
            from aero.agent.loop import AgentLoop
            from aero.toolbox.builtin_tools import download_era5  # noqa: F401

            self.agent = AgentLoop(self.config)
            self._session_id = None
            set_session_id(None)
            self._get_session_mgr()
        except Exception as e:
            self.last_error = str(e)


def _load_config() -> AeroConfig:
    cwd = Path.cwd()
    config_path = cwd / "aero.yaml"
    if config_path.exists():
        return AeroConfig.load(config_path)
    return AeroConfig.create_default()


def _serve(*, port: int, open_browser: bool) -> None:
    """Start the detached local browser workbench through the resident service."""
    import webbrowser

    from aero.core.daemon import DaemonManager

    manager = DaemonManager(Path.cwd())
    result = manager.start(port=port)
    url = manager.browser_url()
    print(f"Aerolytica Web UI: {url}")
    print(f"项目目录: {Path.cwd().resolve()}")
    print(f"服务状态: {'运行中' if result.get('running') else '未运行'}")
    if open_browser:
        with suppress(Exception):
            webbrowser.open(url)


def _save_config(config: AeroConfig) -> None:
    cwd = Path.cwd()
    config.save(cwd / "aero.yaml")


def _preferences_path() -> Path:
    override = os.environ.get("AERO_PREFERENCES_PATH")
    if override:
        return Path(override)
    return Path.home() / ".aero" / "preferences.yaml"


def _load_user_preferences() -> dict:
    path = _preferences_path()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_user_preferences(data: dict) -> None:
    path = _preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))


def _load_saved_theme() -> str:
    ui = _load_user_preferences().get("ui", {})
    if not isinstance(ui, dict):
        return ""
    theme = ui.get("theme", "")
    return str(theme).strip() if theme else ""


def _save_user_theme(theme: str) -> None:
    data = _load_user_preferences()
    ui = data.get("ui")
    if not isinstance(ui, dict):
        ui = {}
    ui["theme"] = theme
    data["ui"] = ui
    _save_user_preferences(data)


def _config_needs_llm_setup(config: AeroConfig) -> bool:
    """A primary model is required before an interactive chat can begin."""
    if config.llm.provider == "official":
        from aero.core.official_account import load_official_session

        return not load_official_session().is_logged_in
    api_key = config.llm.active_api_key()
    return not api_key or api_key.startswith("$")


def _vision_setup_required(status_text: str) -> bool:
    """Identify the structured missing-vision result emitted by analyze_image."""
    return '"setup_required": "vision"' in status_text


def _credential_setup_scope(status_text: str) -> str | None:
    """Read a capability-declared credential request from a tool result."""
    match = re.search(r'"setup_required"\s*:\s*"([a-z_]+)"', status_text)
    if match is None:
        return None
    scope = match.group(1)
    return scope if get_credential_spec(scope) is not None else None


def _extract_llm_api_key(text: str) -> str:
    patterns = [
        r"(?:api\s*key|apikey|key|密钥)\s*[:：=]\s*([A-Za-z0-9][A-Za-z0-9_.-]{7,})",
        r"\b(sk-[A-Za-z0-9_.-]{6,})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip().strip("，,。;；")
    return ""


def _extract_cds_credentials(text: str) -> tuple[str, str] | None:
    """Recognize the official CDS ``url``/``key`` credential format."""
    url_match = re.search(r"(?im)^\s*url\s*[:：=]\s*(https?://\S+)\s*$", text)
    key_match = re.search(r"(?im)^\s*key\s*[:：=]\s*([^\s]+)\s*$", text)
    if url_match is None or key_match is None:
        return None
    url = url_match.group(1).rstrip("/ ")
    key = key_match.group(1).strip().strip("，,。;；")
    if "cds.climate.copernicus.eu" not in url.lower() or not key:
        return None
    return url, key


def _mask_cds_credentials(text: str) -> str:
    credentials = _extract_cds_credentials(text)
    if credentials is None:
        return _mask_secret_text(text)
    _, key = credentials
    return text.replace(key, _mask_secret(key))


def _mask_secret_text(text: str) -> str:
    key = _extract_llm_api_key(text)
    if not key:
        return text
    return text.replace(key, _mask_secret(key))


def _is_bare_api_key(text: str) -> bool:
    key = _extract_llm_api_key(text)
    if not key:
        return False
    stripped = text.strip().strip("'\"`，,。;；")
    return stripped == key


def _mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "..." + value[-4:]


def _mentions_vision_model(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "视觉",
            "vision",
            "图片分析",
            "图像分析",
            "看图",
            "识图",
        )
    )


def _requests_primary_vision_reuse(text: str) -> bool:
    lowered = text.lower()
    reuse_requested = any(
        marker in lowered for marker in ("复用", "共用", "使用已有", "直接用")
    )
    return (
        reuse_requested
        and (
            _mentions_vision_model(text)
            or "复用主模型" in lowered
            or "共用主模型" in lowered
        )
    )


def _mentions_email(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "邮箱",
            "邮件",
            "发邮件",
            "email",
            "smtp",
            "收件人",
            "抄送",
        )
    )


def _mentions_data_credentials(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "merra",
            "merra-2",
            "earthdata",
            "earth data",
            "ges disc",
            "gesdisc",
            "nasa",
            "cams",
            "ads",
            "atmosphere data store",
            "cds",
            "era5",
            "数据源",
            "数据集",
        )
    )


def _mentions_web_search(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "联网搜索",
            "搜索功能",
            "search_web",
            "web search",
            "websearch",
            "search the web",
        )
    )


def _looks_like_llm_setup_intent(text: str) -> bool:
    lowered = text.lower()
    if _mentions_vision_model(text):
        return False
    if _mentions_email(text):
        return False
    if _mentions_data_credentials(text):
        return False
    if _mentions_web_search(text):
        return False
    markers = [
        "api key",
        "apikey",
        "llm",
        "模型",
        "provider",
        "服务商",
        "配置",
        "切换",
        "换 key",
        "qwen",
        "通义",
        "百炼",
        "kimi",
        "moonshot",
        "deepseek",
        "openai",
        "gpt",
    ]
    return any(marker in lowered for marker in markers)


def _parse_llm_clear_from_text(text: str) -> dict | None:
    lowered = text.lower()
    if _mentions_vision_model(text):
        return None
    if _mentions_email(text):
        return None
    if _mentions_data_credentials(text):
        return None
    if _mentions_web_search(text):
        return None
    llm_markers = (
        "api key",
        "apikey",
        "llm",
        "模型",
        "大模型",
        "provider",
        "服务商",
        "deepseek",
        "qwen",
        "通义",
        "百炼",
        "kimi",
        "openai",
        "gpt",
    )
    clear_markers = (
        "清理",
        "清除",
        "清空",
        "删除",
        "移除",
        "删掉",
        "擦掉",
        "重置",
        "reset",
        "clear",
        "remove",
        "delete",
    )
    key_markers = ("key", "密钥", "api")
    if not any(marker in lowered for marker in llm_markers + key_markers):
        return None
    if not any(marker in lowered for marker in clear_markers):
        return None
    reset_provider = any(
        marker in lowered
        for marker in ("完整重置", "全部重置", "重置服务商", "reset provider", "full reset")
    )
    return {"reset_provider": reset_provider}


def _infer_llm_provider_model(text: str, config: AeroConfig) -> tuple[str, str, str]:
    lowered = text.lower()
    current_provider = config.llm.provider or "deepseek"
    provider = current_provider
    model = config.llm.model

    qwen_match = re.search(r"\b(qwen[\w.-]*)\b", lowered)
    if qwen_match or "通义" in text or "百炼" in text:
        provider = "bailian"
        model = qwen_match.group(1) if qwen_match else model or "qwen3.7-plus"
    elif "kimi" in lowered or "moonshot" in lowered or "月之暗面" in text:
        provider = "kimi"
        model_match = re.search(r"\b(kimi-(?:k2|k)[\w.-]*)\b", lowered)
        model = model_match.group(1) if model_match else "kimi-k3"
    elif "deepseek" in lowered or "深度求索" in text:
        provider = "deepseek"
        model_match = re.search(r"\b(deepseek[\w.-]*)\b", lowered)
        model = model_match.group(1) if model_match else model or "deepseek-v4-flash"
    elif "openai" in lowered or re.search(r"\bgpt[\w.-]*\b", lowered):
        provider = "openai"
        model_match = re.search(r"\b(gpt[\w.-]*)\b", lowered)
        model = model_match.group(1) if model_match else model or "gpt-4o"

    provider_alias = model_alias_for_provider(model or "")
    if provider_alias is not None:
        provider, model = provider_alias

    preset = get_provider_preset(provider)
    if preset is not None and not model:
        model = preset.default_model
    base_url = preset.base_url if preset is not None else config.llm.base_url
    return provider, model, base_url


def _parse_llm_setup_from_text(text: str, config: AeroConfig) -> dict | None:
    if _parse_llm_clear_from_text(text) is not None:
        return None
    if not _looks_like_llm_setup_intent(text):
        return None
    provider, model, base_url = _infer_llm_provider_model(text, config)
    api_key = _extract_llm_api_key(text)
    if not api_key and not any(word in text.lower() for word in ("配置", "provider", "api key")):
        return None
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
    }


def _apply_llm_setup(config: AeroConfig, setup: dict) -> None:
    provider = setup["provider"]
    preset = get_provider_preset(provider)
    config.llm.apply_active_provider_defaults()
    config.llm.switch_provider(provider)
    provider_config = config.llm.provider_config(provider)
    provider_config.model = setup.get("model") or (
        preset.default_model if preset else config.llm.model
    )
    provider_config.base_url = (
        setup.get("base_url")
        or (preset.base_url if preset else config.llm.base_url)
    )
    if setup.get("api_key"):
        api_key = _normalize_pasted_api_key(setup["api_key"])
        provider_config.api_key = api_key
        save_llm_profile(provider, api_key, provider_config.model, provider_config.base_url)
    config.llm.use_provider_settings()
    _save_config(config)


def _clear_llm_setup(config: AeroConfig, *, reset_provider: bool = False) -> None:
    previous_provider = config.llm.provider
    config.llm.set_active_api_key("")
    clear_llm_api_key(previous_provider)
    if reset_provider:
        preset = get_provider_preset("deepseek")
        config.llm.switch_provider("deepseek")
        config.llm.model = preset.default_model if preset else "deepseek-v4-flash"
        config.llm.base_url = preset.base_url if preset else "https://api.deepseek.com"
        provider_config = config.llm.provider_config("deepseek")
        provider_config.model = config.llm.model
        provider_config.base_url = config.llm.base_url
        clear_llm_api_key("deepseek")
    _save_config(config)


def _llm_setup_success_message(config: AeroConfig) -> str:
    provider_name = _display_provider_name(config.llm.provider)
    return f"模型服务已配置完成：{provider_name} / {config.llm.model}"


def _llm_clear_success_message(
    config: AeroConfig, *, reset_provider: bool = False
) -> str:
    provider_name = _display_provider_name(config.llm.provider)
    if reset_provider:
        return (
            "模型 API key 已清理，并已重置为默认模型服务。\n\n"
            f"当前模型服务：{provider_name} / {config.llm.model}\n\n"
            "需要继续使用时，把新的 API key 发给我即可。"
        )
    return (
        "模型 API key 已清理。\n\n"
        f"当前仍保留模型服务：{provider_name} / {config.llm.model}\n\n"
        "需要继续使用时，把新的 API key 发给我即可。"
    )


def _is_llm_auth_error(error: Exception) -> bool:
    text = str(error).lower()
    return "401" in text and (
        "unauthorized" in text
        or "未授权" in text
        or "api" in text
        or "llm" in text
    )


def _llm_auth_setup_message(config: AeroConfig) -> str:
    provider_name = _display_provider_name(config.llm.provider)
    preset = get_provider_preset(config.llm.provider)
    lines = [
        f"当前 {provider_name} API key 没有通过服务商验证，已先从本地配置中清空。",
        "",
        f"当前模型服务：{provider_name} / {config.llm.model}",
    ]
    if preset is not None:
        lines.extend(
            [
                "",
                f"你可以到这里创建或复制新的 API key：{preset.api_key_url}",
            ]
        )
    lines.extend(["", "请使用 `/provider` 打开主模型配置界面，不要把 API Key 粘贴到对话框。"])
    return "\n".join(lines)


def _normalize_pasted_api_key(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _resolve_model_alias(provider: str, value: str) -> str:
    model = value.strip()
    if provider == "deepseek":
        return DEEPSEEK_MODEL_ALIASES.get(model, model)
    preset = get_provider_preset(provider)
    if preset and model in ("", "auto", "default"):
        return preset.default_model
    return model


def _normalize_reasoning_effort(value: str) -> str | None:
    effort = value.strip().lower()
    if effort in ("", "off", "none", "auto", "default"):
        return ""
    if effort in REASONING_EFFORTS:
        return effort
    return None


def _model_options(provider: str) -> list[tuple[str, Any]]:
    from aero.cli.setup_wizard import model_option_prompt

    preset = get_provider_preset(provider)
    if preset is None:
        return []
    return [
        (
            model,
            model_option_prompt(
                provider,
                model,
                recommended=model == preset.default_model,
            ),
        )
        for model in preset.models
    ]


def _variant_options() -> list[tuple[str, str]]:
    return [
        ("", "Auto    provider default"),
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("max", "Max"),
        ("xhigh", "XHigh"),
    ]


def _theme_options(available_themes: dict[str, object]) -> list[tuple[str, str]]:
    return [(name, _display_theme_name(name)) for name in available_themes]


def _resolve_theme_name(value: str, available_themes: dict[str, object]) -> str | None:
    normalized = value.strip().lower().replace("_", "-")
    aliases = {
        "dark": "textual-dark",
        "light": "textual-light",
        "auto": "textual-dark",
    }
    candidate = aliases.get(normalized, normalized)
    if candidate in available_themes:
        return candidate
    for name in available_themes:
        if name.lower() == candidate:
            return name
    return None


def _display_theme_name(name: str) -> str:
    return name.replace("-", " ").title()


def _display_model_name(model: str) -> str:
    names = {
        "deepseek-v4-pro": "DeepSeek V4 Pro",
        "deepseek-v4-flash": "DeepSeek V4 Flash",
    }
    return names.get(model, model)


def _display_provider_name(provider: str) -> str:
    preset = get_provider_preset(provider)
    if preset is not None:
        return preset.name
    names = {
        "deepseek": "DeepSeek",
        "openai": "OpenAI",
        "ollama": "Ollama",
    }
    return names.get(provider, provider)


def _display_vision_model_name(model: str) -> str:
    from aero.data.vision_models import VISION_MODELS

    for vid, label in VISION_MODELS:
        if vid == model:
            return label
    return model


def _display_vision_provider_name(provider: str) -> str:
    names = {
        "bailian": "阿里云百炼",
        "openai": "OpenAI",
        "ollama": "Ollama",
    }
    return names.get(provider, _display_provider_name(provider))


def _vision_configured(config: AeroConfig) -> bool:
    return vision_is_configured(config)


def _usage_meta_text(
    tracker: TokenTracker,
    llm_model: str,
    *,
    include_cost: bool = True,
) -> str:
    ctx_win = context_window_for(llm_model)
    ctx_tokens = tracker.current_prompt_tokens or tracker.total_tokens
    context_text = f"[dim]上下文[/dim] {escape(format_token_count(ctx_tokens))}"
    if ctx_win is not None and ctx_win > 0:
        pct = ctx_tokens * 100 / ctx_win
        context_text += f" [dim]/ {pct:.0f}%[/dim]"
    parts = [context_text]
    hit_ratio = tracker.cache_ratio()
    if hit_ratio > 0:
        parts.append(f"[dim]命中缓存[/dim] {hit_ratio:.0%}")
    cost = tracker.total_cost()
    if include_cost and cost > 0:
        parts.append(f"[dim]会话累计[/dim] {escape(format_cost(cost))}")
    return " [dim]|[/dim] " + " [dim]·[/dim] ".join(parts)


def _format_credit_balance(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "未知"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _billing_markdown(tracker: TokenTracker, language: str = "zh") -> str:
    items = tracker.cost_breakdown()
    total = tracker.total_cost()
    zh = language == "zh"
    title = "会话账单" if zh else "Session bill"
    if not items:
        empty = (
            "当前会话尚未产生可计费调用。"
            if zh
            else "No billable usage in this session."
        )
        return f"## {title}\n\n{empty}"

    headers = (
        ("类型", "项目", "用量", "金额", "占比")
        if zh
        else ("Type", "Item", "Usage", "Cost", "Share")
    )
    lines = [
        f"## {title}",
        "",
        f"| {' | '.join(headers)} |",
        "|---|---|---:|---:|---:|",
    ]
    for item in items:
        share = item.cost / total if total > 0 else 0
        lines.append(
            "| "
            + " | ".join(
                (
                    _billing_kind_label(item.kind, zh),
                    _billing_model_or_service_label(item, zh).replace("|", "\\|"),
                    _billing_usage_text(item, zh),
                    _format_bill_cost(item.cost),
                    f"{share:.1%}",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            f"**{'总计' if zh else 'Total'}：{_format_bill_cost(total)}**",
            "",
            (
                "金额按当前目录价估算；供应商免费额度、套餐抵扣与活动优惠"
                "不在本地可见。"
                if zh
                else (
                    "Estimated at current list prices; provider free quotas, "
                    "plans, and promotions are not visible locally."
                )
            ),
        ]
    )
    return "\n".join(lines)


def _billing_kind_label(kind: str, zh: bool) -> str:
    labels = {
        "llm": ("文本模型", "Text model"),
        "vision": ("视觉模型", "Vision model"),
        "service": ("Web Search", "Web Search"),
    }
    pair = labels.get(kind, (kind, kind))
    return pair[0] if zh else pair[1]


def _billing_item_label(name: str, zh: bool) -> str:
    labels = {
        "web_search:bailian": ("百炼 WebSearch MCP", "Bailian WebSearch MCP"),
        "web_search:zhipu": ("智谱 search_std", "Zhipu search_std"),
    }
    pair = labels.get(name)
    if pair is None:
        return name
    return pair[0] if zh else pair[1]


def _billing_model_or_service_label(item, zh: bool) -> str:
    label = _billing_item_label(item.name, zh)
    if item.kind == "service" or not item.provider:
        return label
    return f"{_display_provider_name(item.provider)} / {label}"


def _billing_usage_text(item, zh: bool) -> str:
    if item.kind == "service":
        return f"{item.calls} {'次' if zh else 'calls'}"
    total_tokens = item.prompt_tokens + item.completion_tokens
    text = f"{format_token_count(total_tokens)} tokens"
    if item.cached_tokens:
        cache = format_token_count(item.cached_tokens)
        text += f"（缓存 {cache}）" if zh else f" ({cache} cached)"
    return text


def _format_bill_cost(cost: float) -> str:
    if cost >= 1:
        return f"¥{cost:.2f}"
    if cost >= 0.01:
        return f"¥{cost:.3f}"
    return f"¥{cost:.4f}"


def _is_billing_query(text: str) -> bool:
    """Recognize short, explicit natural-language requests for this session's bill."""
    normalized = re.sub(r"[？?！!。,.，\s]+", "", text.strip().lower())
    if not normalized or len(normalized) > 50:
        return False
    if any(
        marker in normalized
        for marker in ("财务账单", "银行账单", "信用卡账单", "发票", "异常交易")
    ):
        return False
    exact_markers = (
        "账单",
        "费用明细",
        "花费明细",
        "成本明细",
        "消费明细",
        "费用占比",
        "花费占比",
        "成本占比",
        "各模型花费",
        "各模型费用",
        "钱都花在哪里",
        "花了多少钱",
        "用了多少钱",
        "本会话花费",
        "会话花费",
        "查看花费",
        "查看费用",
        "费用是多少",
        "成本是多少",
        "消费情况",
        "sessionbill",
        "costbreakdown",
        "spendbreakdown",
    )
    return any(marker in normalized for marker in exact_markers)


def _session_option_label(name: str, updated_at: float) -> str:
    updated = datetime.fromtimestamp(updated_at).astimezone().isoformat(timespec="minutes")
    return f"{escape(name[:40])} ({escape(updated)})"


def _command_suggestions(
    prefix: str,
    primary: list[tuple[str, str]],
    secondary: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    sources = list(primary)
    # Show child commands as soon as their parent is being typed. Keep the
    # initial "/" menu compact, but do not force users to type a space before
    # discovering options such as `/checkpoints all` or `/session rename`.
    if prefix != "/":
        sources.extend(secondary)
    deduplicated: list[tuple[str, str]] = []
    seen_commands: set[str] = set()
    for command, description in sources:
        normalized = command.rstrip()
        if normalized in seen_commands:
            continue
        seen_commands.add(normalized)
        deduplicated.append((command, description))
    sources = deduplicated
    matched = [(cmd, desc) for cmd, desc in sources if cmd.startswith(prefix)]
    if not matched:
        return []
    exact_idx = next(
        (i for i, (cmd, _) in enumerate(matched) if cmd.rstrip() == prefix.rstrip()),
        None,
    )
    if exact_idx is not None:
        matched.insert(0, matched.pop(exact_idx))
    else:
        matched.sort(key=lambda item: (len(item[0]), item[0]))
    return matched


def _command_requires_input(command: str) -> bool:
    """Return whether Enter should complete this command instead of executing it."""
    return command.rstrip() in {"/set", "/restore", "/experiment", "/paper"}


def _estimate_context_tokens(messages: list[Message]) -> int:
    total_chars = 0
    for msg in messages:
        total_chars += len(msg.role) + len(msg.content)
        if msg.tool_calls:
            total_chars += sum(len(tc.name) + len(_tool_call_arguments_text(tc)) for tc in msg.tool_calls)
    return max(1, (total_chars + 2) // 3)


def _compacted_context_messages(system_msg: Message, summary_text: str) -> list[Message]:
    return [
        system_msg,
        Message(role="user", content=f"[compact_summary]\n{summary_text}"),
        Message(role="assistant", content="OK, I understand the context above."),
    ]


def _is_compact_summary_message(msg: Message) -> bool:
    return msg.role == "user" and (msg.content or "").startswith("[compact_summary]") or \
           msg.role == "user" and (msg.content or "").startswith("[Previous conversation summary]")


def _subagent_task_prompt(task: SubAgentTask) -> str:
    return (
        "你是 Aero 的后台子 agent。只专注完成下面这个后台任务，"
        "不要闲聊，不要询问无关问题。完成后用简洁中文汇报：完成情况、关键结果、"
        "生成或下载的文件路径、是否有失败步骤。\n\n"
        f"任务标题：{task.title}\n\n"
        f"任务说明：\n{task.description}\n\n"
        f"完成标准：\n{task.success_criteria or '完成用户交代的任务，并报告产物路径。'}\n\n"
        f"主对话相关上下文：\n{task.context_summary or '无额外上下文。'}"
    )


def _subagent_title_from_text(text: str, max_len: int = 18) -> str:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return "后台任务"
    return normalized[:max_len] + ("…" if len(normalized) > max_len else "")


def _requests_background_execution(text: str) -> bool:
    normalized = " ".join(text.strip().lower().split())
    if not normalized:
        return False
    query_keywords = ("状态", "进度", "取消", "停止", "中止", "怎么样", "完成了吗")
    if "后台" in normalized and any(keyword in normalized for keyword in query_keywords):
        return False
    patterns = (
        "后台运行",
        "后台执行",
        "后台处理",
        "在后台运行",
        "在后台执行",
        "在后台处理",
        "交给后台",
        "转交后台",
        "丢到后台",
        "background",
    )
    return any(pattern in normalized for pattern in patterns)


def _assistant_claims_background_handoff(text: str) -> bool:
    normalized = " ".join(text.strip().lower().split())
    if not normalized:
        return False
    negative_patterns = (
        "无法转交后台",
        "不能转交后台",
        "无法交给后台",
        "不能交给后台",
        "不交给后台",
        "不在后台",
    )
    if any(pattern in normalized for pattern in negative_patterns):
        return False
    patterns = (
        "交给后台",
        "转交后台",
        "后台处理",
        "后台运行",
        "后台执行",
        "handed to the background",
        "run in the background",
    )
    return any(pattern in normalized for pattern in patterns)


def _is_subagent_tool_status(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "正在转交后台任务",
            "后台任务已启动",
            "后台任务转交失败",
        )
    )


def _collect_artifact_paths(text: str) -> list[str]:
    paths: list[str] = []
    path_pattern = r"(?:figures|data|literature)/[^\s`'\"<>()[\]（）【】，。；;、\\]+"
    for match in re.finditer(path_pattern, text):
        path = match.group(0).rstrip(".,;，。；")
        if path not in paths:
            paths.append(path)
    return paths


def _subagent_done_message(task: SubAgentTask) -> str:
    parts = [
        f"后台任务 #{task.id} 已完成：{task.title}",
        "",
        task.result_summary or "任务已完成。",
    ]
    if task.artifacts:
        parts.extend(["", "产物：", *[f"- {path}" for path in task.artifacts]])
    parts.extend(["", "要查看结果或继续分析吗？"])
    return "\n".join(parts)


def _subagent_context_note(task: SubAgentTask) -> str:
    text = _subagent_done_message(task)
    return f"[后台任务完成摘要]\n{text}"


def _has_pending_tool_calls(messages: list[Message]) -> bool:
    pending_tool_ids: set[str] = set()
    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            pending_tool_ids = {tc.id for tc in msg.tool_calls}
            continue
        if msg.role == "tool" and pending_tool_ids:
            pending_tool_ids.discard(msg.tool_call_id or "")
            continue
        if pending_tool_ids:
            return True
    return bool(pending_tool_ids)


def _sanitize_tool_message_sequence(messages: list[Message]) -> list[Message]:
    sanitized: list[Message] = []
    index = 0
    while index < len(messages):
        msg = messages[index]
        if msg.role == "tool":
            index += 1
            continue

        if msg.role == "assistant" and msg.tool_calls:
            pending_tool_ids = {tc.id for tc in msg.tool_calls}
            tool_messages: list[Message] = []
            seen_tool_ids: set[str] = set()
            cursor = index + 1
            while cursor < len(messages) and messages[cursor].role == "tool":
                tool_msg = messages[cursor]
                tool_call_id = tool_msg.tool_call_id or ""
                if tool_call_id in pending_tool_ids and tool_call_id not in seen_tool_ids:
                    tool_messages.append(tool_msg)
                    seen_tool_ids.add(tool_call_id)
                cursor += 1
            if seen_tool_ids == pending_tool_ids:
                sanitized.append(msg)
                sanitized.extend(tool_messages)
            elif msg.content.strip():
                sanitized.append(Message(role="assistant", content=msg.content))
            index = cursor
            continue

        sanitized.append(msg)
        index += 1
    return sanitized


def _tool_call_arguments_text(tool_call) -> str:
    arguments = getattr(tool_call, "arguments", "")
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments, ensure_ascii=False)


def _session_title_from_messages(messages: list[Message], max_len: int = 24) -> str:
    candidates = [
        _clean_session_title_text(m.content)
        for m in messages
        if m.role == "user" and m.content.strip()
    ]
    for text in candidates:
        if text and not _is_low_information_session_title(text):
            return _truncate_session_title(text, max_len)
    if candidates:
        return _truncate_session_title(candidates[0], max_len)
    return ""


def _has_persistable_session_messages(messages: list[Message]) -> bool:
    """Return whether an agent conversation contains an actual user request."""
    return any(
        message.role == "user"
        and message.content.strip()
        and not message.content.startswith("[compact_summary]")
        for message in messages
    )


def _checkpoint_title_from_messages(messages: list[Message], max_len: int = 18) -> str:
    """Build a short deterministic fallback from the latest useful request."""
    for message in reversed(messages):
        if message.role != "user" or not message.content.strip():
            continue
        text = _clean_session_title_text(message.content)
        if text and not _is_low_information_session_title(text):
            return _truncate_session_title(text, max_len)
    return ""


def _checkpoint_title_prompt(messages: list[Message], language: str) -> str:
    """Ask the model for a compact title describing the latest work state."""
    transcript: list[str] = []
    for message in messages:
        if message.role not in {"user", "assistant"} or not message.content.strip():
            continue
        role = "用户" if message.role == "user" else "Aero"
        content = _clean_session_title_prompt_text(message.content)
        if content:
            transcript.append(f"{role}: {content[:350]}")
    context = "\n".join(transcript[-6:]) or "当前尚无有效对话内容"
    if language == "zh":
        return (
            "请根据最近的工作内容，为当前检查点生成一个简短标题。\n"
            "要求：概括当前完成的工作或阶段；6到16个中文字符；"
            "不要使用‘检查点’三个字；不要加引号、句号或解释。\n\n"
            f"{context}\n\n标题："
        )
    return (
        "Create a short title for the current checkpoint from the latest work below.\n"
        "Requirements: describe the completed work or current stage in 3 to 7 words; "
        "do not use the word 'checkpoint'; no quotes, explanation, or trailing period.\n\n"
        f"{context}\n\nTitle:"
    )


def _experiment_report_prompt(
    experiment: dict,
    messages: list[Message],
    artifacts: list[str],
    memo_context: str = "",
) -> str:
    transcript: list[str] = []
    for message in messages:
        if message.role not in {"user", "assistant"} or not message.content.strip():
            continue
        role = "用户" if message.role == "user" else "Aero"
        content = message.content.strip()[:700]
        transcript.append(f"{role}: {content}")
    conversation = "\n".join(transcript[-20:]) or "无有效对话记录"
    artifact_text = "\n".join(f"- {path}" for path in artifacts[:200]) or "- 无"
    memo_text = memo_context.strip() or "无"
    return (
        f"请为实验“{experiment['name']}”撰写一份简洁、可独立阅读的 Markdown 实验报告。\n"
        "必须包含：目标、方法与过程、关键结果、结论、局限或未解决问题、产物清单。"
        "只能依据下面的对话、产物和研究备忘录，不得编造结果；没有证据的内容明确写为未确认。"
        "备忘录是候选结论，必须结合其中的依据和实验内容判断是否采用，并保留相关备忘录 ID。"
        "不要提及内部工具名，也不要添加报告之外的寒暄。\n\n"
        f"实验对话：\n{conversation}\n\n产物：\n{artifact_text}\n\n"
        f"研究备忘录：\n{memo_text}"
    )


def _fallback_experiment_report(
    experiment: dict, messages: list[Message], artifacts: list[str]
) -> str:
    latest_request = ""
    for message in reversed(messages):
        if message.role == "user" and message.content.strip():
            latest_request = message.content.strip().splitlines()[0][:160]
            break
    artifact_lines = "\n".join(f"- `{path}`" for path in artifacts) or "- 无已记录产物"
    return (
        f"# 实验报告：{experiment['name']}\n\n"
        "## 目标\n\n"
        f"{latest_request or '当前对话中没有可提取的明确实验目标。'}\n\n"
        "## 结果与结论\n\n"
        "自动总结未能完成，请结合下列实验产物补充结论。\n\n"
        f"## 产物\n\n{artifact_lines}\n\n"
        "## 未解决问题\n\n- 实验结果尚需人工确认。"
    )


def _normalize_checkpoint_title(title: str) -> str:
    """Normalize a generated title and remove generic checkpoint wording."""
    title = _normalize_generated_session_title(title, max_len=18)
    title = re.sub(r"检查点|checkpoint", "", title, flags=re.I)
    return _normalize_generated_session_title(title, max_len=18)


def _session_title_prompt(messages: list[Message], language: str) -> str:
    transcript = []
    seen_user = False
    for msg in messages:
        if msg.role not in {"user", "assistant"} or not msg.content.strip():
            continue
        if msg.role == "assistant" and not seen_user:
            continue
        if msg.role == "user" and seen_user:
            break
        role = "用户" if msg.role == "user" else "Aero"
        content = _clean_session_title_prompt_text(msg.content)
        if content:
            transcript.append(f"{role}: {content}")
        if msg.role == "user":
            seen_user = True
        elif msg.role == "assistant":
            break
    text = "\n".join(transcript) or "无有效对话内容"
    if language == "zh":
        return (
            "请根据下面第一轮对话为这个会话起一个简短标题。\n"
            "要求：中文优先，8到18个字；不要加引号；不要解释；不要句号。\n\n"
            f"{text}\n\n标题："
        )
    return (
        "Create a short title for this chat from the first exchange below.\n"
        "Requirements: 3 to 8 words, no quotes, no explanation, no trailing period.\n\n"
        f"{text}\n\nTitle:"
    )


def _clean_session_title_prompt_text(text: str) -> str:
    text = strip_image_markdown(text)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def _normalize_generated_session_title(title: str, max_len: int = 24) -> str:
    title = title.strip()
    title = title.splitlines()[0] if title else ""
    title = re.sub(r"^(?:标题|title)\s*[:：]\s*", "", title, flags=re.I)
    title = title.strip(" \t\r\n\"'“”‘’`*#。，、；：:,.!?！？")
    title = re.sub(r"\s+", " ", title)
    if not title:
        return ""
    return _truncate_session_title(title, max_len)


def _clean_session_title_text(text: str) -> str:
    text = strip_image_markdown(text)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[/\\][^\s，。！？；：,!?;:]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" \t\r\n\"'“”‘’。，、；：:,.!?！？")
    text = re.sub(
        r"^(?:请|帮我|麻烦|能不能|可以|能否|请帮我|帮忙|我想|我要|给我)\s*",
        "",
        text,
    ).strip()
    text = re.sub(r"^(?:please|can you|could you|help me|i want to)\s+", "", text, flags=re.I)
    text = text.strip(" \t\r\n\"'“”‘’。，、；：:,.!?！？")
    return text


def _is_low_information_session_title(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text).lower()
    if not normalized:
        return True
    greetings = {
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "hello",
        "hi",
        "hey",
        "在吗",
        "test",
        "测试",
    }
    if normalized in greetings:
        return True
    return len(normalized) <= 2 and not re.search(r"[\u4e00-\u9fff]", normalized)


def _truncate_session_title(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _help_text(lang: str) -> str:
    help_lines = [
        t("help.shortcuts", lang),
        t("help.shortcuts.updn", lang),
        t("help.shortcuts.pgupdn", lang),
        t("help.shortcuts.ctrls", lang),
        t("help.shortcuts.ctrlq", lang),
        t("help.shortcuts.tab", lang),
        t("help.shortcuts.escape", lang),
        "",
        t("help.slash_title", lang),
        t("help.slash_quit", lang),
        t("help.slash_clear", lang),
        t("help.slash_help", lang),
        t("help.slash_copy", lang),
        t("help.slash_new", lang),
        t("help.slash_language", lang),
        t("help.slash_model", lang),
        t("help.slash_preview", lang),
        t("help.slash_provider", lang),
        t("help.slash_theme", lang),
        t("help.slash_variants", lang),
        t("help.slash_vision", lang),
        "  /websearch [on|off|status]  管理网页搜索开关并查看状态",
        t("help.slash_bill", lang),
        t("help.slash_mode", lang),
        t("help.slash_session", lang),
        t("help.slash_session_rename", lang),
        t("help.slash_checkpoint", lang),
        t("help.slash_checkpoint_delete", lang),
        t("help.slash_checkpoint_rename", lang),
        t("help.slash_checkpoints", lang),
        t("help.slash_checkpoints_clear", lang),
        t("help.slash_restore", lang),
        t("help.slash_paper", lang),
        t("help.slash_experiment", lang),
        t("help.slash_experiment_delete", lang),
        t("help.slash_experiment_finish", lang),
        t("help.slash_experiments", lang),
        t("help.slash_experiments_clear", lang),
        t("help.slash_compact", lang),
        t("help.slash_set", lang),
        t("help.slash_revoke", lang),
        t("help.slash_revoke_item", lang),
        "",
        t("help.footer", lang),
    ]
    return "\n".join(help_lines)


def _checkpoint_tool_ledger(messages: list[Message]) -> list[dict[str, Any]]:
    """Build a redacted ledger of tool calls and their recorded outcomes."""
    outcomes = {
        message.tool_call_id: message.content
        for message in messages
        if message.role == "tool" and message.tool_call_id
    }
    ledger = []
    for message in messages:
        for call in message.tool_calls or []:
            outcome = outcomes.get(call.id, "")
            ledger.append(
                {
                    "tool": call.name,
                    "arguments": _redact_checkpoint_value(call.arguments),
                    "status": (
                        "success" if outcome and "error" not in outcome.lower() else "unknown"
                    ),
                    "outputs": _checkpoint_output_paths(outcome),
                }
            )
    return ledger


def _redact_checkpoint_value(value: Any, key: str = "") -> Any:
    sensitive = {"api_key", "apikey", "key", "password", "secret", "token"}
    lowered = key.lower()
    if lowered in sensitive or any(part in lowered for part in ("password", "secret", "token")):
        return "***"
    if isinstance(value, dict):
        return {str(k): _redact_checkpoint_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_checkpoint_value(item) for item in value]
    return value


def _checkpoint_output_paths(content: str) -> list[str]:
    """Extract only project file paths from a tool result; never persist raw output."""
    if not content:
        return []
    candidates: list[str] = []
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        value = None

    def visit(item: Any, key: str = "") -> None:
        if isinstance(item, dict):
            for child_key, child in item.items():
                visit(child, str(child_key))
        elif isinstance(item, list):
            for child in item:
                visit(child, key)
        elif isinstance(item, str) and any(
            part in key.lower() for part in ("file", "path", "output")
        ):
            path = Path(item)
            if not path.is_absolute() and ".." not in path.parts:
                candidates.append(path.as_posix())

    visit(value)
    return sorted(set(candidates))[:100]


def _format_checkpoint_diff(checkpoint: dict, diff) -> str:
    lines = [f"### 恢复预览：{checkpoint['name']}", ""]
    groups = (
        ("将覆盖", diff.modified),
        ("将恢复", diff.missing),
        ("将移除", diff.added),
        ("仅记录的数据已变化（不会覆盖）", diff.references_changed),
    )
    for label, paths in groups:
        lines.append(f"**{label}：{len(paths)}**")
        lines.extend(f"- `{path}`" for path in paths[:20])
        if len(paths) > 20:
            lines.append(f"- 以及另外 {len(paths) - 20} 个文件")
        lines.append("")
    if not diff.has_changes:
        lines.append("当前工作区与该检查点一致。")
    elif checkpoint.get("exact_restore"):
        lines.append("确认后才会修改文件；大数据文件只报告状态。")
    else:
        lines.append("该检查点只保存了数据清单，不能恢复文件。")
    return "\n".join(lines)


def _format_checkpoint_list_entry(checkpoint: dict[str, Any]) -> str:
    """Format a compact user-facing checkpoint list row."""
    created = datetime.fromtimestamp(checkpoint["created_at"]).strftime("%Y-%m-%d %H:%M")
    return f"- `{checkpoint['id']}` · **{checkpoint['name']}** · {created}"


def _restore_confirmation_message(checkpoint: dict, diff) -> str:
    lines = [f"恢复到「{checkpoint['name']}」后：", ""]
    changes = (
        ("覆盖当前修改", diff.modified),
        ("找回已删除文件", diff.missing),
        ("删除此后新增文件", diff.added),
    )
    changed = False
    for label, paths in changes:
        if not paths:
            continue
        changed = True
        lines.append(f"{label}（{len(paths)} 个）：")
        lines.extend(f"  • {path}" for path in paths[:8])
        if len(paths) > 8:
            lines.append(f"  • 以及另外 {len(paths) - 8} 个文件")
        lines.append("")
    if not changed:
        lines.extend(["项目文件无需变更，将恢复当时的对话和工作状态。", ""])
    if diff.references_changed:
        lines.extend(
            [
                f"有 {len(diff.references_changed)} 个数据文件与当时不同，内容将保持不变。",
                "",
            ]
        )
    lines.extend(
        [
            "data/ 和大型科研数据不会被覆盖或删除。",
            "恢复前会保存当前状态为恢复保护记录，之后仍可撤回。",
        ]
    )
    return "\n".join(lines)


def _is_replaceable_status(text: str) -> bool:
    return _status_progress_slot(text) is not None


def _is_same_status_slot(old: str, new: str) -> bool:
    old_slot = _status_progress_slot(old)
    return old_slot is not None and old_slot == _status_progress_slot(new)


def _is_completed_download_progress(text: str) -> bool:
    return text.startswith(("下载进度#", "下载进度 ")) and bool(re.search(r"\b100(?:\.0+)?%", text))


def _status_progress_slot(text: str) -> str | None:
    if text.startswith("下载进度#"):
        return text.split(" ", 1)[0]
    if text.startswith("下载进度 "):
        return "下载进度"
    if text.startswith("GCS ARCO ") and "，已等待 " in text:
        return text.split("，已等待 ", 1)[0]
    return None


def _display_status_line(text: str) -> str:
    if text.startswith("下载进度#") and " " in text:
        return "下载进度 " + text.split(" ", 1)[1]
    if text.startswith("stdout: "):
        return "命令输出：" + text.removeprefix("stdout: ")
    if text.startswith("stderr: "):
        content = text.removeprefix("stderr: ")
        prefix = "错误输出：" if _stderr_line_looks_like_error(content) else "命令日志："
        return prefix + content
    return text


def _stderr_line_looks_like_error(text: str) -> bool:
    line = text.strip()
    if not line:
        return False
    lowered = line.lower()
    # Matplotlib emits font fallback notices to stderr even when a figure was
    # successfully saved. They are actionable diagnostics, not command errors.
    if lowered.startswith("findfont:"):
        return False
    if "download completed" in lowered:
        return False
    if re.match(r"^\d{4}-\d{2}-\d{2} .*?\b(info|debug)\b", line, flags=re.IGNORECASE):
        return False
    if re.search(r"\b(info|debug)\b", line, flags=re.IGNORECASE):
        return False
    error_markers = (
        "traceback",
        "exception",
        "error",
        "failed",
        "failure",
        "no such file or directory",
        "permission denied",
        "invalid request",
        "bad request",
        "command not found",
        "not found",
    )
    return any(marker in lowered for marker in error_markers)


def _normalize_confirm_choice(choice: str) -> str:
    if choice in {"allow", "approve", "always", "deny"}:
        return choice
    if choice == "defer":
        debug_log("tui.confirm_deferred", mapping="defer->deny")
        return "deny"
    debug_log("tui.confirm_unexpected_choice", choice=choice)
    return "deny"


def _should_queue_input_during_run(state: AgentRunState | None) -> bool:
    return state is None or state.phase in {"thinking", "text"}


def _activity_indicator(frame_index: int) -> str:
    frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    return frames[frame_index % len(frames)]


def _render_status_lines(lines: list[str], activity: str | None = None) -> str:
    rendered: list[str] = []
    last_index = len(lines) - 1
    for index, line in enumerate(lines):
        prefix = f"{activity} " if activity is not None and index == last_index else "  "
        rendered.append(f"{prefix}{escape(_display_status_line(line))}")
    return "\n".join(rendered)


def _render_terminal_math(text: str) -> str:
    text = re.sub(
        r"\\\[\s*(?P<body>.*?)\s*\\\]",
        lambda m: _latex_math_to_text(m.group("body")),
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\\\(\s*(?P<body>.*?)\s*\\\)",
        lambda m: _latex_math_to_text(m.group("body")),
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\$\$\s*(?P<body>.*?)\s*\$\$",
        lambda m: _latex_math_to_text(m.group("body")),
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"(?<!\$)\$(?!\$)\s*(?P<body>.*?)(?<!\$)\$(?!\$)",
        lambda m: _latex_math_to_text(m.group("body")),
        text,
        flags=re.DOTALL,
    )
    text = _repair_terminal_math_text(text)
    return _normalize_terminal_markdown_emphasis(text)


def _normalize_terminal_markdown_emphasis(text: str) -> str:
    """Make CJK-adjacent strong emphasis parse in Textual Markdown.

    Textual treats ``中文**重点**后文`` as intraword delimiters and renders the
    asterisks literally. Minimal boundary spaces make the emphasis unambiguous.
    Code spans and fences stay intact.
    """
    rendered: list[str] = []
    in_fence = False
    fence_marker = ""
    for line in text.splitlines(keepends=True):
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)[0]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            rendered.append(line)
            continue
        if in_fence:
            rendered.append(line)
            continue

        parts = re.split(r"(`+[^`\n]*`+)", line)
        for index in range(0, len(parts), 2):
            parts[index] = _repair_cjk_strong_emphasis(parts[index])
        rendered.append("".join(parts))
    return "".join(rendered)


def _repair_cjk_strong_emphasis(text: str) -> str:
    matches = list(re.finditer(r"\*\*(?=\S).+?(?<=\S)\*\*", text))
    if not matches:
        return text
    rendered: list[str] = []
    cursor = 0
    for match in matches:
        rendered.append(text[cursor : match.start()])
        if match.start() > 0 and not text[match.start() - 1].isspace():
            rendered.append(" ")
        rendered.append(match.group())
        if match.end() < len(text) and not text[match.end()].isspace():
            rendered.append(" ")
        cursor = match.end()
    rendered.append(text[cursor:])
    return "".join(rendered)


def _latex_math_to_text(expr: str) -> str:
    expr = expr.strip()
    replacements = {
        r"\,": " ",
        r"\;": " ",
        r"\:": " ",
        r"\!": "",
        r"\quad": " ",
        r"\qquad": " ",
        r"\times": "×",
        r"\cdot": "·",
        r"\pm": "±",
        r"\leq": "≤",
        r"\le": "≤",
        r"\geq": "≥",
        r"\ge": "≥",
        r"\neq": "≠",
        r"\approx": "≈",
        r"\alpha": "α",
        r"\beta": "β",
        r"\gamma": "γ",
        r"\lambda": "λ",
        r"\mu": "μ",
        r"\pi": "π",
        r"\phi": "φ",
        r"\theta": "θ",
        r"\tau": "τ",
        r"\sigma": "σ",
        r"\omega": "ω",
        r"\Delta": "Δ",
        r"\Phi": "Φ",
        r"\Theta": "Θ",
        r"\delta": "δ",
        r"\partial": "∂",
        r"\sum": "Σ",
        r"\int": "∫",
        r"\cos": "cos",
        r"\sin": "sin",
        r"\tan": "tan",
        r"\exp": "exp",
        r"\log": "log",
        r"\infty": "∞",
        r"\downarrow": "↓",
        r"\uparrow": "↑",
        r"\rightarrow": "→",
        r"\to": "→",
        r"\left": "",
        r"\right": "",
    }
    for src, dst in replacements.items():
        expr = expr.replace(src, dst)

    expr = expr.replace(r"\ ", " ")
    expr = re.sub(r"\\(?:text|mathrm|mathbf|operatorname)\{([^{}]*)\}", r"\1", expr)
    expr = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"(\1)/(\2)", expr)
    expr = re.sub(r"\\sqrt\{([^{}]+)\}", r"√(\1)", expr)
    expr = re.sub(r"\^\{([^{}]+)\}", r"^\1", expr)
    expr = re.sub(r"\^\{?2\}?", "²", expr)
    expr = re.sub(r"\^\{?3\}?", "³", expr)
    expr = re.sub(r"_\{([^{}]+)\}", r"_\1", expr)
    expr = re.sub(r"_([A-Za-z0-9])(?!\.)", lambda m: _subscript_char(m.group(1)), expr)
    expr = re.sub(r"\\([A-Za-z]+)", r"\1", expr)
    expr = expr.replace("{", "").replace("}", "")
    expr = expr.replace(" ,", ",")
    expr = re.sub(r"\s+", " ", expr).strip()
    return expr


def _repair_terminal_math_text(text: str) -> str:
    text = re.sub(r"\bsqrt\s*int\b", "√∫", text)
    text = re.sub(r"\bsqrt(?=∫)", "√", text)
    return text


def _subscript_char(value: str) -> str:
    subscripts = {
        "0": "₀",
        "1": "₁",
        "2": "₂",
        "3": "₃",
        "4": "₄",
        "5": "₅",
        "6": "₆",
        "7": "₇",
        "8": "₈",
        "9": "₉",
        "a": "ₐ",
        "e": "ₑ",
        "h": "ₕ",
        "i": "ᵢ",
        "j": "ⱼ",
        "k": "ₖ",
        "l": "ₗ",
        "m": "ₘ",
        "n": "ₙ",
        "o": "ₒ",
        "p": "ₚ",
        "r": "ᵣ",
        "s": "ₛ",
        "t": "ₜ",
        "u": "ᵤ",
        "v": "ᵥ",
        "x": "ₓ",
    }
    return subscripts.get(value, f"_{value}")


def _sanitize_user_facing_text(text: str) -> str:
    replacements = {
        "inspect_nc": "进一步检查文件内容",
        "inspect_grib2": "进一步检查 GRIB2 文件内容",
        "download_era5": "下载数据",
        "download_gfs": "下载 GFS 预报数据",
        "check_gfs_availability": "检查 GFS 可用时次",
        "search_cds_variables": "查询变量信息",
        "search_gfs_variables": "查询 GFS 可用要素",
        "lookup_gfs_parameter": "查阅 GFS 要素定义",
        "lookup_ecmwf_parameter": "查阅 ECMWF 参数定义",
        "get_gefs_forecast_schedule": "解析 GEFS 预报时效",
        "check_gefs_availability": "检查 GEFS 可用时次",
        "download_gefs": "下载 GEFS 集合预报数据",
        "search_gefs_variables": "查询 GEFS 可用要素",
        "lookup_gefs_parameter": "查阅 GEFS 要素定义",
        "download_ifs": "下载 IFS 预报数据",
        "check_ifs_availability": "检查 IFS 可用时次",
        "search_ifs_variables": "查询 IFS 可用要素",
        "get_ifs_forecast_schedule": "解析 IFS 预报时效",
        "retry_download": "重试下载",
        "list_downloads": "查看下载记录",
        "query_download": "查询下载状态",
        "configure_cds_key": "保存 CDS 配置",
        "check_cds_config": "检查 CDS 配置",
        "cleanup_downloads": "清理下载记录",
        "list_figures": "查看图片列表",
        "check_vision_model_config": "检查视觉模型配置",
        "analyze_image": "分析图片",
        "configure_vision_model": "配置视觉模型",
        "configure_email_config": "配置邮箱",
        "check_email_config": "检查邮箱配置",
        "send_email": "发送邮件",
    }
    for name, label in replacements.items():
        text = text.replace(f"`{name}`", label)
        text = text.replace(name, label)
    return text


def _run_llm_setup_wizard(config: AeroConfig) -> bool:
    provider = "deepseek"
    preset = get_provider_preset(provider)
    model = preset.default_model if preset else "deepseek-v4-flash"
    base_url = preset.base_url if preset else "https://api.deepseek.com"

    print()
    print("欢迎使用 Aero")
    print("首次启动需要配置 DeepSeek API key。")
    print(f"默认使用：DeepSeek V4 ({model})")
    print()
    if preset is not None:
        print(f"API key 获取入口：{preset.api_key_url}")
        print(preset.api_key_hint)
    else:
        print(f"API key 获取入口：{DEEPSEEK_API_KEYS_URL}")
        print("打开 DeepSeek 开放平台，在 API keys 页面创建并复制 sk- 开头的 key。")
    print()
    print("后续如果要更换其他模型，可以直接用自然语言描述，让 Aero 替你完成配置。")
    print("例如：帮我切换到 Kimi K2.6，或者配置 qwen3.7。")
    print()

    api_key = input("请粘贴 DeepSeek API key: ").strip()
    if not api_key:
        return False

    _apply_llm_setup(
        config,
        {
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
        },
    )
    print(_llm_setup_success_message(config))
    print("API key 已保存到 ~/.aero/secrets.yaml")
    print()
    return True


def main():
    log_path = configure_debug_logging()
    standard_log_path = log_path.with_name("aero.log")
    configure_logging(log_level="WARNING", log_file=str(standard_log_path))

    if len(sys.argv) < 2:
        _print_usage()
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd in {"service", "daemon", "web"}:
        from aero.cli.daemon import main as daemon_main

        daemon_main((["web"] if cmd == "web" else []) + sys.argv[2:])
        return

    if cmd == "chat":
        chat_args = sys.argv[2:]
        removed_modes = [arg for arg in chat_args if arg in {"--simple", "--no-tui"}]
        if removed_modes:
            print(
                f"参数 {removed_modes[0]} 已移除；aero chat 现在只提供 Textual TUI 模式。"
            )
            sys.exit(2)
        allowed_args = {"--mouse", "--no-mouse", "--continue", "-c", "--service", "--attach"}
        unknown_args = [arg for arg in chat_args if arg not in allowed_args]
        if unknown_args:
            print(f"未知参数: {unknown_args[0]}")
            _print_usage()
            sys.exit(2)
        mouse_mode = "--no-mouse" not in sys.argv
        resume_last_session = "--continue" in sys.argv or "-c" in sys.argv
        debug_log(
            "cli.chat",
            tui_mode=True,
            mouse_mode=mouse_mode,
            resume_last_session=resume_last_session,
            debug_log_path=str(log_path),
            standard_log_path=str(standard_log_path),
        )

        if "--service" in chat_args or "--attach" in chat_args:
            from aero.cli.attached_chat import AttachedChatApp
            from aero.core.daemon import DaemonManager

            manager = DaemonManager(Path.cwd())
            manager.start()
            info = manager.connection_info()
            AttachedChatApp(info["base_url"], info["token"]).run(mouse=mouse_mode)
        else:
            config = _load_config()
            app = AeroApp(config, resume_last_session=resume_last_session)
            app.run(mouse=mouse_mode)

    elif cmd == "serve":
        serve_args = sys.argv[2:]
        port = 8765
        index = 0
        while index < len(serve_args):
            arg = serve_args[index]
            if arg == "--no-open":
                index += 1
                continue
            if arg == "--port" and index + 1 < len(serve_args):
                try:
                    port = int(serve_args[index + 1])
                except ValueError:
                    print("--port 必须是整数")
                    sys.exit(2)
                index += 2
                continue
            if arg.startswith("--port="):
                try:
                    port = int(arg.split("=", 1)[1])
                except ValueError:
                    print("--port 必须是整数")
                    sys.exit(2)
                index += 1
                continue
            print(f"未知参数: {arg}")
            _print_usage()
            sys.exit(2)
        if not 1 <= port <= 65535:
            print("端口必须在 1 到 65535 之间")
            sys.exit(2)
        _serve(port=port, open_browser="--no-open" not in serve_args)

    elif cmd == "init":
        _init()
    elif cmd == "setup":
        from aero.cli.init_runtime import setup_runtime

        setup_args = sys.argv[2:]
        unknown = [
            arg
            for arg in setup_args
            if arg not in {"--full", "--yes", "-y", "--china-mirror"}
        ]
        if unknown:
            print(f"未知参数: {unknown[0]}")
            _print_usage()
            sys.exit(2)
        if not setup_runtime(
            full="--full" in setup_args,
            assume_yes="--yes" in setup_args or "-y" in setup_args,
            china_mirror="--china-mirror" in setup_args,
        ):
            sys.exit(1)
    elif cmd == "doctor":
        from aero.cli.init_runtime import runtime_diagnostics

        healthy, checks = runtime_diagnostics()
        print("Aero 运行环境检查")
        for label, passed, detail in checks:
            print(f"  {'✓' if passed else '✗'} {label}: {detail}")
        if not healthy:
            print("\n运行 aero setup 修复基础运行时。")
            sys.exit(1)
    elif cmd == "runtime":
        runtime_args = sys.argv[2:]
        if not runtime_args or runtime_args[0] != "clean":
            print("用法: aero runtime clean [--yes]")
            sys.exit(2)
        unknown = [arg for arg in runtime_args[1:] if arg not in {"--yes", "-y"}]
        if unknown:
            print(f"未知参数: {unknown[0]}")
            sys.exit(2)
        from aero.cli.init_runtime import clean_runtime

        if not clean_runtime(assume_yes="--yes" in runtime_args or "-y" in runtime_args):
            sys.exit(1)
    elif cmd == "agent":
        from aero.cli.agent_runner import run_agent_cli

        exit_code = run_agent_cli(sys.argv[2:])
        if exit_code:
            sys.exit(exit_code)
    elif cmd in ("-v", "--version", "version"):
        print("Aero 0.1.0")
    elif cmd in ("-h", "--help", "help"):
        _print_usage()
    else:
        print(f"未知命令: {cmd}")
        _print_usage()
        sys.exit(1)


def _print_usage():
    print("""
Aero — 气象科研 AI Agent IDE

命令:
  aero init            初始化当前目录的项目配置与工作目录
  aero setup           安装独立的基础科学运行时（Python 3.12）
  aero setup --full    预装完整科学计算工具集
  aero setup --full --china-mirror  强制使用国内 Conda/PyPI 镜像
  aero doctor          检查 Aero 私有运行时
  aero runtime clean   删除 Aero 私有运行时（不影响项目和用户 Conda）
  aero agent register --name ocean [--description "备注"]  注册远程 Agent
  aero agent list       列出本地 Agent 及在线状态
  aero agent run [智能体名称或智能体 ID] [--cloud-memory]  前台常驻并等待云端指令
  aero agent status [Agent 名称或 Agent ID]  查询指定 Agent 状态
  aero chat            启动 Textual TUI 对话（支持中文输入和流式输出）
  aero chat --service 通过本机常驻服务续接会话，关闭 TUI 不会终止任务
  aero chat --continue 续接当前目录上一次保存的会话（短参数: -c）
  aero serve           启动本地 Web Agent 工作台（默认端口 8765）
  aero serve --port N  使用指定端口启动本地 Web 工作台
  aero serve --no-open 启动服务但不自动打开浏览器
  aero chat --mouse    启用 Textual 鼠标滚轮（默认已启用，保留兼容）
  aero chat --no-mouse 禁用 Textual 鼠标模式，交给终端原生选择/复制
  ↑/↓ 或 PageUp/PageDown  对话中滚动聊天区域
  /copy                   对话中复制最后一条 Aero 回复到剪贴板
  /model flash|pro|chat   对话中切换模型（也可传完整模型名）
  /provider deepseek|kimi 对话中切换模型服务商
  /theme dark|light      对话中切换 TUI 主题（也可传完整主题名）
  /variants low|medium|high|max|auto 对话中设置推理强度
  /language              对话中切换语言, Agent 以设定语言回复
   /set max_tool_rounds N  对话中设置当前会话最大工具调用轮次（默认 999）
  /revoke                 查看「一直允许」的工具列表
  /revoke <tool>          撤销某工具的「一直允许」
  aero version         显示版本号
  aero help            显示此帮助信息
""")




def _init():
    cwd = Path.cwd()
    config_path = cwd / "aero.yaml"

    if config_path.exists():
        config = AeroConfig.load(config_path)
        _create_workspace_dirs(cwd, config.output.data_dir)
        print(f"当前目录已初始化: {cwd}")
        print(f"   配置文件: {config_path}")
        print(f"   数据目录: {cwd / config.output.data_dir}")
        return

    print(f"在当前目录初始化 Aero 工作区: {cwd}")
    config = AeroConfig.create_default()

    config.save(config_path)

    _create_workspace_dirs(cwd, config.output.data_dir)

    print(f"当前目录已初始化: {config_path}")
    print(f"   数据目录: {cwd / config.output.data_dir}")
    print()
    print("   运行环境: 使用 aero setup 单独安装，不修改用户的 Conda")
    print()
    print("启动对话: aero chat")


def _create_workspace_dirs(root: Path, data_dir: str = "data") -> None:
    for relative_path in (data_dir, "figures", "scripts/tmp", "plans", "literature"):
        (root / relative_path).mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    main()
