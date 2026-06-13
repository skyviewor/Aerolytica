"""Aero CLI — Textual TUI and command dispatcher."""

import asyncio
import copy
import json
import os
import re
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

from rich.markup import escape
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Offset
from textual.screen import ModalScreen
from textual.scrollbar import ScrollBarRender
from textual.widgets import (
    Button,
    ListItem,
    ListView,
    Markdown,
    OptionList,
    Static,
    TextArea,
)
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
from aero.core.config import (
    AeroConfig,
    clear_llm_api_key,
    save_llm_profile,
)
from aero.core.debug_log import configure_debug_logging, debug_log
from aero.core.llm_providers import (
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

DEEPSEEK_MODEL_ALIASES = {
    "flash": "deepseek-v4-flash",
    "v4-flash": "deepseek-v4-flash",
    "pro": "deepseek-v4-pro",
    "v4-pro": "deepseek-v4-pro",
    "chat": "deepseek-chat",
    "reasoner": "deepseek-reasoner",
}
REASONING_EFFORTS = {"low", "medium", "high", "max", "xhigh"}
DEEPSEEK_API_KEYS_URL = "https://platform.deepseek.com/api_keys"
DEEPSEEK_DOCS_URL = "https://api-docs.deepseek.com/"

STARTUP_LOGO = "── A E R O L Y T I C A ──"

_CONFIRM_ACTION_LABELS = {
    "delete_file": "删除文件",
    "run_shell": "执行 Shell 命令",
    "ensure_runtime_tools": "安装命令行工具",
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
        Binding("enter", "confirm_selected", "确认", show=False, priority=True),
        Binding("space", "confirm_selected", "确认", show=False, priority=True),
    ]

    _BUTTON_IDS = ("#btn-allow", "#btn-always", "#btn-deny")

    def __init__(self, message: str, lang: str = "zh"):
        super().__init__()
        self._message = message
        self._lang = lang
        self._selected_button = 0

    def on_mount(self) -> None:
        self._sync_selected_button()

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(t("confirm.permission_required", self._lang), id="confirm-title")
            with VerticalScroll(id="confirm-message-box"):
                yield Static(self._message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Static(t("confirm.allow_once", self._lang), id="btn-allow", classes="confirm-option")
                yield Static(t("confirm.allow_always", self._lang), id="btn-always", classes="confirm-option")
                yield Static(t("confirm.reject", self._lang), id="btn-deny", classes="confirm-option")
                yield Static(t("confirm.shortcuts", self._lang), id="confirm-shortcuts")

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
        if event.character == "A":
            event.stop()
            self.action_always()

    @on(events.Click, "#btn-allow")
    def on_allow_pressed(self) -> None:
        self._selected_button = 0
        self.action_allow()

    @on(events.Click, "#btn-always")
    def on_always_pressed(self) -> None:
        self._selected_button = 1
        self.action_always()

    @on(events.Click, "#btn-deny")
    def on_deny_pressed(self) -> None:
        self._selected_button = 2
        self.action_deny()

    def action_focus_previous(self) -> None:
        self._move_button_focus(-1)

    def action_focus_next(self) -> None:
        self._move_button_focus(1)

    def action_confirm_selected(self) -> None:
        self._confirm_selected_button()

    def _move_button_focus(self, delta: int) -> None:
        self._selected_button = (self._selected_button + delta) % len(self._BUTTON_IDS)
        self._sync_selected_button()

    def _sync_selected_button(self) -> None:
        for index, selector in enumerate(self._BUTTON_IDS):
            button = self.query_one(selector, Static)
            selected = index == self._selected_button
            button.set_class(selected, "confirm-selected")
            button.refresh(repaint=True)

    def _confirm_selected_button(self) -> None:
        if self._selected_button == 0:
            self.action_allow()
        elif self._selected_button == 1:
            self.action_always()
        else:
            self.action_deny()

    def action_allow(self) -> None:
        self.dismiss("allow")

    def action_always(self) -> None:
        self.dismiss("always")

    def action_deny(self) -> None:
        self.dismiss("deny")


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
        options: list[tuple[str, str]],
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

    def _option_prompt(self, value: str, label: str) -> str:
        selected = "●" if (value or "auto") == (self._current or "auto") else " "
        return f"{selected} {label}"


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


class ChatMarkdown(Markdown):
    """Markdown body that lets chat history own wheel scrolling."""

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

    def __init__(self, config: AeroConfig, persist_config: bool = True):
        super().__init__()
        self.config = config
        self.persist_config = persist_config
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
        self._filtered_commands: list[tuple[str, str]] = []
        self._cmd_list: ListView | None = None
        self._model_info: Static | None = None
        self._input_meta: Static | None = None
        self._footer_status: Static | None = None
        self._footer_status_token = 0
        self._footer_temp_text = ""
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

    def _set_input_focus_style(self, active: bool) -> None:
        input_box = self.query_one("#input-box")
        input_box.set_class(active, "input-active")

    def on_mouse_down(self, event: events.MouseDown) -> None:
        input_box = self.query_one("#input-box")
        if input_box.region.contains(event.screen_x, event.screen_y):
            self.call_later(self.query_one("#user-input", TextArea).focus)

    def on_text_selected(self, event: events.TextSelected) -> None:
        selected_text = self.screen.get_selected_text()
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
        if _vision_configured(self.config):
            text += t("app.vision_line", lang).format(
                model=_display_vision_model_name(self.config.vision.model),
                provider=_display_vision_provider_name(self.config.vision.provider),
            )
        return f"[dim]{text}[/dim]" if markup else text

    def _input_meta_text(self) -> str:
        model_name = _display_model_name(self.config.llm.model)
        provider = _display_provider_name(self.config.llm.provider)
        effort = self.config.llm.reasoning_effort or "auto"
        text = (
            f"{escape(model_name)} [dim]{escape(provider)} ·[/dim] "
            f"[bold warning]{escape(effort)}[/bold warning]"
        )
        if _vision_configured(self.config):
            vision_model = _display_vision_model_name(self.config.vision.model)
            vision_provider = _display_vision_provider_name(self.config.vision.provider)
            text += (
                f" [dim]&[/dim] {escape(vision_model)} "
                f"[dim]{escape(vision_provider)}[/dim]"
            )
        if self.agent is not None and self.agent.tracker.total_tokens > 0:
            text += _usage_meta_text(
                self.agent.tracker,
                self.config.llm.model,
                self.config.vision.model,
            )
        return text

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

        llm_clear = _parse_llm_clear_from_text(text)
        if llm_clear is not None:
            await self._handle_local_llm_clear(text, reset_provider=llm_clear["reset_provider"])
            self.query_one("#user-input", TextArea).focus()
            return

        llm_setup = _parse_llm_setup_from_text(text, self.config)
        if llm_setup is not None:
            await self._handle_local_llm_setup(text, llm_setup)
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
            preset = get_provider_preset(setup["provider"])
            if preset is None:
                message = "请提供这个模型服务的 API key，或使用 /provider 选择内置服务商。"
            else:
                message = (
                    f"已识别为 {_display_provider_name(setup['provider'])} / {setup['model']}。\n\n"
                    f"请到这里创建或复制 API key：{preset.api_key_url}\n\n"
                    "拿到后直接粘贴给我即可，我会在本地保存配置。"
                )
            await agent_msg.update(message)
            self._chat_log.append(f"Aero:\n{message}")
            return

        _apply_llm_setup(self.config, setup)
        self._refresh_agent_llm_config()
        self._refresh_model_info()
        message = _llm_setup_success_message(self.config)
        await agent_msg.update(message)
        self._chat_log.append(f"Aero:\n{message}")
        self._set_footer_status(message)

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
        search_dirs = [Path("."), Path("data"), Path(".") / "data"]
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
                    p = (Path(".") / p).resolve()
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
            ("/mode", t("cmd.mode", self.config.language)),
            ("/session", t("cmd.session", self.config.language)),
            ("/compact", t("cmd.compact", self.config.language)),
            ("/instructions", t("cmd.instructions", self.config.language)),
            ("/subagent", t("cmd.subagent", self.config.language)),
        ]
        self._secondary_commands_list = [
            ("/set max_tool_rounds ", "设置最大工具调用轮次"),
            ("/set model ", "切换模型  如 flash 或 pro"),
            ("/set variants ", "设置推理强度  low/medium/high/max/auto"),
            ("/session rename ", "修改当前会话标题"),
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
            self.push_screen(
                SelectScreen(
                    t("app.vision_select_title", lang),
                    vision_model_options(),
                    self.config.vision.model,
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
        if selected is not None:
            self._set_vision_model(selected)
        self.query_one("#user-input", TextArea).focus()

    def _set_vision_model(self, model: str) -> None:
        lang = self.config.language
        self.config.vision.model = model
        if self.persist_config:
            _save_config(self.config)
        self._refresh_model_info()
        self._set_footer_status(
            t("app.vision_switched", lang).format(model=model)
        )

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
        if self.agent is None or len(self.agent.messages) <= 1:
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
        self._session_id = session_id
        set_session_id(session_id)
        self._pending_session_title = ""
        self._enter_chat_mode()
        chat.remove_children()
        self._mount_chat_title()
        self._render_loaded_session_messages(messages)
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

    def _render_loaded_session_messages(self, messages: list[Message]) -> None:
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

    def _mount_agent_message_sync(self, text: str) -> None:
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
        stack.mount(ChatMarkdown(strip_image_markdown(text), classes="agent-content"))
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
        if self.agent is not None:
            self.agent.llm.config.model = model
        if self.persist_config:
            _save_config(self.config)
        self._refresh_model_info()
        self._set_footer_status(t("app.model_switched", lang).format(
            provider=self.config.llm.provider, model=model))

    def _set_provider(self, value: str) -> None:
        lang = self.config.language
        previous_provider = self.config.llm.provider
        provider = normalize_provider_id(value)
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
        if self.agent is not None:
            self.agent.llm.config.provider = self.config.llm.provider
            self.agent.llm.config.base_url = self.config.llm.base_url
            self.agent.llm.config.model = self.config.llm.model
            self.agent.llm.config.api_key = self.config.llm.active_api_key()
            self.agent.config.llm.provider = self.config.llm.provider
            self.agent.config.llm.base_url = self.config.llm.base_url
            self.agent.config.llm.model = self.config.llm.model
            self.agent.config.llm.set_active_api_key(self.config.llm.active_api_key())
        if self.persist_config:
            _save_config(self.config)
        self._refresh_model_info()
        status = t("app.provider_switched", lang).format(
            provider=_display_provider_name(provider),
            model=self.config.llm.model,
        )
        if previous_provider != provider and not self.config.llm.active_api_key():
            status += "，当前服务商还没有 API key"
        self._set_footer_status(status)

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
        if cmd.endswith(" "):
            inp.load_text(cmd)
        elif cmd == "/set":
            inp.load_text("/set max_tool_rounds ")
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
        if cmd.endswith(" "):
            inp.load_text(cmd)
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
                use_subagent_launcher(self._launch_sub_agent_from_tool),
                use_subagent_status_provider(self._query_sub_agents_from_tool),
                use_subagent_canceller(self._cancel_sub_agent_from_tool),
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
            screen = ConfirmScreen(message, self.config.language)
            return await self.push_screen_wait(screen)
        finally:
            self.screen.remove_class("confirming")

    async def _handle_confirm(self, content: str) -> None:
        choice = await self._show_confirm_dialog(content)
        debug_log("tui.confirm_dialog_closed", choice=choice)
        choice = _normalize_confirm_choice(choice)
        if choice in {"approve", "allow", "always"}:
            if self.config.mode == "plan":
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
    return not config.llm.api_key or config.llm.api_key.startswith("$")


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


def _mask_secret_text(text: str) -> str:
    key = _extract_llm_api_key(text)
    if not key:
        return text
    return text.replace(key, _mask_secret(key))


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


def _looks_like_llm_setup_intent(text: str) -> bool:
    lowered = text.lower()
    if _mentions_vision_model(text):
        return False
    if _mentions_email(text):
        return False
    if _mentions_data_credentials(text):
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
        model = qwen_match.group(1) if qwen_match else model or "qwen-plus"
    elif "kimi" in lowered or "moonshot" in lowered or "月之暗面" in text:
        provider = "kimi"
        model_match = re.search(r"\b(kimi-(?:k2|k)[\w.-]*)\b", lowered)
        model = model_match.group(1) if model_match else "kimi-k2.6"
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
    lines.extend(["", "拿到新的 key 后直接粘贴给我，我会帮你保存配置。"])
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


def _model_options(provider: str) -> list[tuple[str, str]]:
    preset = get_provider_preset(provider)
    if preset is not None:
        return [
            (model, f"{model}    {preset.name}")
            for model in preset.models
        ]
    if provider == "deepseek":
        return [
            ("deepseek-v4-pro", "DeepSeek V4 Pro    DeepSeek"),
            ("deepseek-v4-flash", "DeepSeek V4 Flash  DeepSeek"),
            ("deepseek-chat", "DeepSeek Chat      DeepSeek"),
            ("deepseek-reasoner", "DeepSeek Reasoner  DeepSeek"),
        ]
    return []


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
        "deepseek-chat": "DeepSeek Chat",
        "deepseek-reasoner": "DeepSeek Reasoner",
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
    return bool(config.vision.model and config.vision.api_key)


def _usage_meta_text(tracker: TokenTracker, llm_model: str, vision_model: str) -> str:
    ctx_win = context_window_for(llm_model)
    ctx_tokens = tracker.current_prompt_tokens or tracker.total_tokens
    pct = ctx_tokens * 100 / ctx_win if ctx_win > 0 else 0
    parts = [
        (
            "[dim]上下文[/dim] "
            f"{escape(format_token_count(ctx_tokens))} "
            f"[dim]/ {pct:.0f}%[/dim]"
        )
    ]
    hit_ratio = tracker.cache_ratio()
    if hit_ratio > 0:
        parts.append(f"[dim]命中缓存[/dim] {hit_ratio:.0%}")
    cost = tracker.total_cost(llm_model, vision_model)
    if cost > 0:
        parts.append(escape(format_cost(cost)))
    return " [dim]|[/dim] " + " [dim]·[/dim] ".join(parts)


def _session_option_label(name: str, updated_at: float) -> str:
    updated = datetime.fromtimestamp(updated_at).astimezone().isoformat(timespec="minutes")
    return f"{escape(name[:40])} ({escape(updated)})"


def _command_suggestions(
    prefix: str,
    primary: list[tuple[str, str]],
    secondary: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    sources = list(primary)
    if " " in prefix:
        sources.extend(secondary)
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
        t("help.slash_mode", lang),
        t("help.slash_session", lang),
        t("help.slash_session_rename", lang),
        t("help.slash_compact", lang),
        t("help.slash_set", lang),
        t("help.slash_revoke", lang),
        t("help.slash_revoke_item", lang),
        "",
        t("help.footer", lang),
    ]
    return "\n".join(help_lines)


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
    return text


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

    if cmd == "chat":
        simple_mode = "--simple" in sys.argv or "--no-tui" in sys.argv
        tui_mode = not simple_mode
        mouse_mode = "--no-mouse" not in sys.argv
        debug_log(
            "cli.chat",
            tui_mode=tui_mode,
            simple_mode=simple_mode,
            mouse_mode=mouse_mode,
            debug_log_path=str(log_path),
            standard_log_path=str(standard_log_path),
        )

        config = _load_config()

        if _config_needs_llm_setup(config):
            if not _run_llm_setup_wizard(config):
                print("未完成模型配置，退出。")
                sys.exit(1)

        if tui_mode:
            app = AeroApp(config)
            app.run(mouse=mouse_mode)
        else:
            import asyncio

            asyncio.run(_chat_simple(config))

    elif cmd == "init":
        _init()
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
  aero init            初始化当前目录的配置与工作目录
  aero chat            启动 Textual TUI 对话（支持中文输入和流式输出）
  aero chat --simple   启动纯文本对话
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


async def _chat_simple(config: AeroConfig):
    from aero.agent.loop import AgentLoop
    from aero.toolbox.builtin_tools import download_era5  # noqa: F401

    loop = AgentLoop(config)
    last_reply_text = ""

    print("  Aero v0.1.0")
    print(f"  模型: {config.llm.provider}/{config.llm.model}")
    print(f"  强度: {config.llm.reasoning_effort or 'auto'}")
    print(f"  工作目录: {Path.cwd()}")
    print("  /quit 退出, /clear 清除上下文, /copy 复制最后回复")
    print("  /provider 切换模型服务商, /model 切换模型, /variants 设置强度")
    print("  /set max_tool_rounds N 设置工具轮次")
    print("  /language zh|en 切换语言")
    print()

    try:
        while True:
            user_input = input("> ").strip()
            if not user_input:
                continue
            if user_input == "/language" or user_input.startswith("/language "):
                parts = user_input.split(maxsplit=1)
                if len(parts) == 1:
                    label = {"zh": "中文", "en": "English"}.get(config.language, config.language)
                    print(
                        f"当前语言: {label} ({config.language})。"
                        "用法: /language zh 或 /language en"
                    )
                    continue
                value = parts[1].strip()
                if not is_supported_language(value):
                    print(t("app.language_bad", value))
                    continue
                label = {"zh": "中文", "en": "English"}[value]
                config.language = value
                loop.reset_system_prompt(value)
                _save_config(config)
                print(f"语言已切换为 {label}")
                continue
            if user_input == "/quit":
                print("再见!")
                break
            if user_input == "/clear":
                loop.messages = [loop.messages[0]]
                last_reply_text = ""
                print("上下文已清除。")
                continue
            if user_input == "/copy":
                if not last_reply_text.strip():
                    print("暂无可复制的回复")
                    continue
                try:
                    subprocess.run(
                        ["pbcopy"],
                        input=last_reply_text,
                        text=True,
                        check=True,
                    )
                    print("已复制最后一条回复")
                except Exception as e:
                    print(f"复制失败: {e}")
                continue
            if user_input == "/provider" or user_input.startswith("/provider "):
                parts = user_input.split(maxsplit=1)
                if len(parts) == 1:
                    print(f"当前模型服务商: {_display_provider_name(config.llm.provider)}")
                    print("内置服务商:")
                    for provider_id, label in provider_options():
                        print(f"  {provider_id:<12} {label}")
                    print("用法: /provider deepseek 或 /provider kimi")
                    continue
                provider = normalize_provider_id(parts[1])
                preset = get_provider_preset(provider)
                if preset is None:
                    print("未知模型服务商。请使用内置服务商，或在 aero.yaml 中自定义。")
                    continue
                previous_provider = config.llm.provider
                config.llm.apply_active_provider_defaults()
                config.llm.switch_provider(provider)
                provider_config = config.llm.provider_config(provider)
                if not provider_config.base_url:
                    provider_config.base_url = preset.base_url
                if not provider_config.model:
                    provider_config.model = preset.default_model
                config.llm.use_provider_settings()
                loop.config.llm.provider = provider
                loop.config.llm.base_url = config.llm.base_url
                loop.config.llm.model = config.llm.model
                loop.config.llm.set_active_api_key(config.llm.active_api_key())
                loop.llm.config.provider = provider
                loop.llm.config.base_url = config.llm.base_url
                loop.llm.config.model = config.llm.model
                loop.llm.config.api_key = config.llm.active_api_key()
                _save_config(config)
                print(f"模型服务已切换为 {preset.name}/{config.llm.model}")
                if previous_provider != provider and not config.llm.active_api_key():
                    print("当前服务商还没有 API key。")
                    print(f"API key 获取入口: {preset.api_key_url}")
                continue
            llm_clear = _parse_llm_clear_from_text(user_input)
            if llm_clear is not None:
                _clear_llm_setup(config, reset_provider=llm_clear["reset_provider"])
                loop.config.llm.provider = config.llm.provider
                loop.config.llm.model = config.llm.model
                loop.config.llm.providers = config.llm.providers
                loop.config.llm.base_url = config.llm.base_url
                loop.llm.config.provider = config.llm.provider
                loop.llm.config.model = config.llm.model
                loop.llm.config.api_key = config.llm.active_api_key()
                loop.llm.config.base_url = config.llm.base_url
                print(
                    _llm_clear_success_message(
                        config,
                        reset_provider=llm_clear["reset_provider"],
                    )
                )
                continue
            llm_setup = _parse_llm_setup_from_text(user_input, config)
            if llm_setup is not None:
                if not llm_setup.get("api_key"):
                    preset = get_provider_preset(llm_setup["provider"])
                    if preset is None:
                        print("请提供这个模型服务的 API key，或使用 /provider 选择内置服务商。")
                    else:
                        print(
                            f"已识别为 {_display_provider_name(llm_setup['provider'])} "
                            f"/ {llm_setup['model']}"
                        )
                        print(f"API key 获取入口: {preset.api_key_url}")
                    continue
                _apply_llm_setup(config, llm_setup)
                loop.config.llm.provider = config.llm.provider
                loop.config.llm.model = config.llm.model
                loop.config.llm.providers = config.llm.providers
                loop.config.llm.base_url = config.llm.base_url
                loop.llm.config.provider = config.llm.provider
                loop.llm.config.model = config.llm.model
                loop.llm.config.api_key = config.llm.active_api_key()
                loop.llm.config.base_url = config.llm.base_url
                print(_llm_setup_success_message(config))
                continue
            if user_input == "/model" or user_input.startswith("/model "):
                parts = user_input.split(maxsplit=1)
                if len(parts) == 1:
                    aliases = ", ".join(
                        f"{k}={v}" for k, v in DEEPSEEK_MODEL_ALIASES.items()
                    )
                    print(f"当前模型: {config.llm.provider}/{config.llm.model}")
                    print(f"DeepSeek 快捷名: {aliases}")
                    print("用法: /model flash 或 /model deepseek-v4-pro")
                    continue
                model = _resolve_model_alias(config.llm.provider, parts[1])
                if not model:
                    print("模型名不能为空")
                    continue
                config.llm.model = model
                loop.llm.config.model = model
                _save_config(config)
                print(f"模型已切换为 {config.llm.provider}/{model}")
                continue
            if (
                user_input == "/variants"
                or user_input.startswith("/variants ")
                or user_input == "/effort"
                or user_input.startswith("/effort ")
            ):
                parts = user_input.split(maxsplit=1)
                if len(parts) == 1:
                    print(
                        f"当前推理强度: {config.llm.reasoning_effort or 'auto'}；"
                        "用法: /variants low|medium|high|max|auto"
                    )
                    continue
                effort = _normalize_reasoning_effort(parts[1])
                if effort is None:
                    print("推理强度可选: low / medium / high / max / auto")
                    continue
                config.llm.reasoning_effort = effort
                loop.llm.config.reasoning_effort = effort
                _save_config(config)
                print(f"推理强度已设置为 {effort or 'auto'}")
                continue
            if user_input.startswith("/set "):
                parts = user_input.split()
                if len(parts) < 3:
                    print("用法: /set max_tool_rounds 20 或 /set model flash")
                    continue
                if parts[1] == "model":
                    model = _resolve_model_alias(config.llm.provider, parts[2])
                    if not model:
                        print("模型名不能为空")
                        continue
                    config.llm.model = model
                    loop.llm.config.model = model
                    _save_config(config)
                    print(f"模型已切换为 {config.llm.provider}/{model}")
                    continue
                if parts[1] in ("variants", "effort", "reasoning_effort"):
                    effort = _normalize_reasoning_effort(parts[2])
                    if effort is None:
                        print("推理强度可选: low / medium / high / max / auto")
                        continue
                    config.llm.reasoning_effort = effort
                    loop.llm.config.reasoning_effort = effort
                    _save_config(config)
                    print(f"推理强度已设置为 {effort or 'auto'}")
                    continue
                if len(parts) != 3 or parts[1] != "max_tool_rounds":
                    print("用法: /set max_tool_rounds 20")
                    continue
                try:
                    value = int(parts[2])
                except ValueError:
                    print("max_tool_rounds 必须是整数")
                    continue
                if value < 1:
                    print("max_tool_rounds 必须 >= 1")
                    continue
                loop.max_tool_rounds = value
                from aero.toolbox.builtin_tools import set_max_tool_rounds
                set_max_tool_rounds(value)
                config.max_tool_rounds = value
                _save_config(config)
                print(f"max_tool_rounds 已设置为 {value}")
                continue
            if user_input == "/revoke" or user_input.startswith("/revoke "):
                parts = user_input.split(maxsplit=1)
                if len(parts) == 1:
                    if not loop.always_allow:
                        print("当前没有「一直允许」的工具")
                    else:
                        tools = ", ".join(sorted(loop.always_allow))
                        print(f"一直允许的工具: {tools}")
                        print("用法: /revoke <tool_name> 撤销")
                else:
                    tool_name = parts[1].strip()
                    if tool_name in loop.always_allow:
                        loop.always_allow.discard(tool_name)
                        print(f"已撤销 {tool_name} 的「一直允许」")
                    else:
                        print(f"{tool_name} 不在「一直允许」列表中")
                continue

            if user_input == "/help" or user_input == "help":
                print()
                print("Aero 帮助")
                print("─" * 40)
                print()
                print("斜杠命令:")
                print("  /quit         退出")
                print("  /clear        清除上下文")
                print("  /help         显示帮助")
                print("  /copy         复制上次回复")
                print("  /model        查看/切换模型，如 /model flash")
                print("  /variants     查看/设置推理强度: low/medium/high/max/auto")
                print("  /set max_tool_rounds N  设置最大工具轮次 (默认 999)")
                print("  /revoke       查看一直允许的工具")
                print("  /revoke <t>   撤销一直允许")
                print()
                print("快捷键:")
                print("  Ctrl+Q  退出")
                print("  ↑/↓    滚动（仅 TUI）")
                print()
                continue

            print()
            response_text = ""
            async for event in loop.run_stream(user_input):
                if event.type == "text":
                    response_text += event.content
                    print(event.content, end="", flush=True)
                elif event.type == "confirm":
                    pass
                elif event.type == "done":
                    print()
                    print("───")
                    print()
            if response_text:
                last_reply_text = response_text

    except KeyboardInterrupt:
        print("\n再见!")
    finally:
        await loop.close()


def _init():
    from aero.cli.init_runtime import setup_runtime

    cwd = Path.cwd()
    config_path = cwd / "aero.yaml"

    if config_path.exists():
        config = AeroConfig.load(config_path)
        _create_workspace_dirs(cwd, config.output.data_dir)
        print(f"当前目录已初始化: {cwd}")
        print(f"   配置文件: {config_path}")
        print(f"   数据目录: {cwd / config.output.data_dir}")
        setup_runtime()
        return

    print(f"在当前目录初始化 Aero 工作区: {cwd}")
    config = AeroConfig.create_default()

    config.save(config_path)

    _create_workspace_dirs(cwd, config.output.data_dir)

    print(f"当前目录已初始化: {config_path}")
    print(f"   数据目录: {cwd / config.output.data_dir}")
    print()
    setup_runtime()
    print()
    print("启动对话: aero chat")


def _create_workspace_dirs(root: Path, data_dir: str = "data") -> None:
    for relative_path in (data_dir, "figures", "scripts/tmp", "plans", "literature"):
        (root / relative_path).mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    main()
