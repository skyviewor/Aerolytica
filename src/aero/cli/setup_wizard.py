"""First-run Textual setup wizard for Aero model capabilities."""

# TUI copy and CSS intentionally include long, user-facing lines.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
from typing import Any

from rich.table import Table
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Input, OptionList, Static
from textual.widgets._option_list import Option

from aero.core.llm_providers import (
    BUILTIN_LLM_PROVIDERS,
    BYOK_UI_ENABLED,
    OFFICIAL_ACCOUNT_UI_ENABLED,
    model_supports_vision,
    model_tags,
)
from aero.core.types import Message


def model_option_prompt(
    provider: str,
    model: str,
    *,
    recommended: bool = False,
) -> Table:
    """Build the shared model ID, capability, positioning, and recommendation row."""
    model_type, positioning = model_tags(provider, model)
    prompt = Table.grid(expand=True, padding=0)
    prompt.add_column(width=22, no_wrap=True)
    prompt.add_column(width=8, no_wrap=True)
    prompt.add_column(ratio=1, no_wrap=True)
    prompt.add_column(width=4, justify="right", no_wrap=True)
    prompt.add_row(model, model_type, positioning, "推荐" if recommended else "")
    return prompt


def _model_option(provider: str, model: str, *, recommended: bool = False) -> Option:
    """Build a selectable model option while preserving the raw model ID."""
    return Option(
        model_option_prompt(provider, model, recommended=recommended),
        id=model,
    )


def _vision_option(provider_id: str, model: str, preset_name: str) -> Option:
    """Build a vision-model option carrying provider info in its id."""
    model_type, positioning = model_tags(provider_id, model)
    prompt = Table.grid(expand=True, padding=0)
    prompt.add_column(width=10, no_wrap=True)
    prompt.add_column(width=22, no_wrap=True)
    prompt.add_column(width=8, no_wrap=True)
    prompt.add_column(ratio=1, no_wrap=True)
    prompt.add_row(preset_name, model, model_type, positioning)
    return Option(prompt, id=f"{provider_id}|{model}")


class FirstRunSetupScreen(Screen[dict[str, Any] | None]):
    """Full-screen first-run setup for primary and optional visual capability."""

    CSS = """
    FirstRunSetupScreen {
        background: #161616 100%;
        align: center middle;
    }

    #setup-dialog {
        width: 76;
        max-width: 92%;
        height: auto;
        max-height: 36;
        background: $surface;
        border: none;
        padding: 1 2 0 2;
    }

    #setup-title {
        text-style: bold;
        width: 100%;
        color: $text;
    }

    #setup-subtitle {
        color: $text-muted;
        margin: 0 0 1 0;
    }

    #setup-choice-slot {
        width: 100%;
        height: auto;
    }

    #setup-list {
        height: auto;
        max-height: 15;
        border: none;
        background: transparent;
        padding: 0;
    }

    #setup-list:focus {
        border: none;
    }

    #setup-list.setup-list-hidden {
        height: 0;
        border: none;
        margin: 0;
        padding: 0;
    }

    #setup-form {
        height: auto;
    }

    .setup-label {
        margin: 1 0 0 0;
        color: $text-muted;
    }

    Input.setup-field {
        margin: 0 0 1 0;
        padding: 1 1 0 1;
        border: none;
        background: #202020;
    }

    Input.setup-field:focus {
        border: none;
        padding: 1 1 0 1;
        background: #282828;
    }

    #setup-provider-help {
        margin: 0 0 1 0;
        padding: 1;
        background: #202020;
        color: $text-muted;
    }

    #setup-actions {
        width: 100%;
        height: 3;
        margin: 1 0 0 0;
        background: transparent;
        padding: 0;
        align: left middle;
    }

    .setup-action {
        width: auto;
        min-width: 8;
        padding: 0 1;
        margin: 0 1 0 0;
        background: transparent;
        color: $text-muted;
        content-align: center middle;
    }

    .setup-action.setup-selected {
        background: transparent;
        color: #5dade2;
        text-style: bold;
    }

    #setup-error {
        color: $error;
        margin-top: 1;
    }

    #setup-error.setup-progress {
        color: $text-muted;
    }

    #setup-error.setup-success {
        color: $success;
    }

    #setup-hint {
        width: 100%;
        height: 1;
        margin: 0 0 1 0;
        content-align: right middle;
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "取消"),
        Binding("tab", "navigate_tab(1)", show=False, priority=True),
        Binding("shift+tab", "navigate_tab(-1)", show=False, priority=True),
    ]
    _FORM_INPUT_IDS = ("#setup-url", "#setup-model", "#setup-key")
    _ACTION_IDS = ("#setup-next", "#setup-skip", "#setup-back", "#setup-cancel")
    _CONNECTION_SUCCESS_DELAY = 1.0

    def __init__(
        self,
        *,
        vision_only: bool = False,
        primary_only: bool = False,
        primary: dict[str, Any] | None = None,
        primary_supports_vision: bool = False,
    ) -> None:
        super().__init__()
        self._vision_only = vision_only
        self._primary_only = primary_only
        self._page = (
            "vision_mode"
            if vision_only
            else "primary_form"
            if primary_only
            else "source"
        )
        if not BYOK_UI_ENABLED:
            self._page = "official"
        self._primary: dict[str, Any] = dict(primary or {})
        self._primary_supports_vision = primary_supports_vision
        self._vision: dict[str, Any] = {"mode": "unconfigured"}
        self._web_search: dict[str, Any] = {"configured": False}
        self._selected_action = 0
        self._action_focused = False
        self._verifying_primary = False
        self._verifying_vision = False
        self._verifying_web_search = False

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-dialog"):
            yield Static("欢迎使用 Aerolytica", id="setup-title")
            yield Static("", id="setup-subtitle")
            yield Static("", id="setup-error")
            yield Vertical(id="setup-choice-slot")
            with Vertical(id="setup-form"):
                yield Input(placeholder="接口地址（Endpoint）", id="setup-url", classes="setup-field")
                yield Input(placeholder="模型 ID", id="setup-model", classes="setup-field")
                yield Static("", id="setup-provider-help")
                yield Input(placeholder="API Key", id="setup-key", password=True, classes="setup-field")
            with Horizontal(id="setup-actions"):
                yield Static("继续", id="setup-next", classes="setup-action")
                yield Static("跳过", id="setup-skip", classes="setup-action")
                yield Static("返回", id="setup-back", classes="setup-action")
                yield Static("取消", id="setup-cancel", classes="setup-action")
            yield Static("Tab 切换 · ↑↓ 内容 · ←→ 操作 · Enter 确认", id="setup-hint")

    def on_mount(self) -> None:
        self.call_after_refresh(self._render_page)

    @on(OptionList.OptionSelected, "#setup-list")
    def on_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._choose(str(event.option.id))

    @on(events.Click, "#setup-next")
    def on_next(self) -> None:
        self._selected_action = 0
        self._activate_selected_action()

    @on(events.Click, "#setup-skip")
    def on_skip(self) -> None:
        self._selected_action = 1
        self._activate_selected_action()

    @on(events.Click, "#setup-back")
    def on_back(self) -> None:
        self._selected_action = 2
        self._activate_selected_action()

    @on(events.Click, "#setup-cancel")
    def on_cancel_button(self) -> None:
        self._selected_action = 3
        self._activate_selected_action()

    def on_key(self, event: events.Key) -> None:
        if self._action_focused:
            if event.key in {"left", "right", "tab", "shift+tab"}:
                event.stop()
                event.prevent_default()
                delta = -1 if event.key in {"left", "shift+tab"} else 1
                self._move_action_focus(delta)
            elif event.key == "up":
                event.stop()
                event.prevent_default()
                self._leave_actions()
            elif event.key in {"enter", "space"}:
                event.stop()
                event.prevent_default()
                self._activate_selected_action()
            return

        options = self._current_option_list()
        if options is not None and self.focused is options:
            highlighted = options.highlighted
            at_last_option = (
                highlighted is not None
                and highlighted == options.option_count - 1
            )
            if (event.key == "down" and at_last_option) or event.key == "tab":
                event.stop()
                event.prevent_default()
                self._focus_actions()
            return

        if self._page in {"official", "primary_form", "vision_form", "web_search_form"}:
            if event.key in {"up", "down", "tab", "shift+tab"}:
                event.stop()
                event.prevent_default()
                # A delayed cleanup event from a formerly focused OptionList
                # may leave the screen without a focusable widget for one
                # frame. Recover first, rather than interpreting that key as
                # navigation from an invisible list.
                if not isinstance(self.focused, Input):
                    self._focus_first_visible_field()
                    return
                delta = -1 if event.key in {"up", "shift+tab"} else 1
                self._move_form_focus(delta)
            elif event.key == "enter":
                event.stop()
                event.prevent_default()
                self._submit_form()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_navigate_tab(self, delta: int) -> None:
        """Cycle through visible fields and custom actions in display order."""
        fields = (
            self._visible_input_ids()
            if self._page in {"official", "primary_form", "vision_form", "web_search_form"}
            else []
        )
        options = self._current_option_list()
        content = fields or (["#setup-list"] if options is not None else [])
        actions = [self._ACTION_IDS[index] for index in self._visible_action_indices()]
        targets = content + actions
        if not targets:
            return
        current = (
            self._ACTION_IDS[self._selected_action]
            if self._action_focused
            else f"#{self.focused.id}" if self.focused is not None else None
        )
        if current in targets:
            target = targets[(targets.index(current) + delta) % len(targets)]
        else:
            target = targets[0 if delta > 0 else -1]
        if target in actions:
            self._selected_action = self._ACTION_IDS.index(target)
            self._focus_actions()
        else:
            self._action_focused = False
            self.set_focus(self.query_one(target))
            self._sync_selected_action()

    def _render_page(self) -> None:
        title = self.query_one("#setup-title", Static)
        subtitle = self.query_one("#setup-subtitle", Static)
        error = self.query_one("#setup-error", Static)
        form = self.query_one("#setup-form")
        choice_pages = {
            "source",
            "primary_provider",
            "primary_model",
            "vision_mode",
            "vision_model",
            "web_search_setup",
        }
        choice_page = self._page in choice_pages
        choice_slot = self.query_one("#setup-choice-slot", Vertical)
        try:
            options = choice_slot.query_one("#setup-list", OptionList)
        except Exception:
            options = None
        if choice_page and options is None:
            options = OptionList(id="setup-list")
            choice_slot.mount(options)
        elif not choice_page and options is not None:
            # Keep the list mounted while it is hidden. Removing the focused
            # OptionList defers focus cleanup inside Textual; that later
            # cleanup can steal focus back from an input when the user returns
            # from a visual-model choice page to this form.
            options.clear_options()
        choice_slot.display = choice_page
        choice_slot.styles.display = "block" if choice_page else "none"
        choice_slot.styles.height = "auto" if choice_page else 0
        if options is not None:
            options.display = choice_page
            options.clear_options()

        error.remove_class("setup-success", "setup-progress")
        error.update("")
        form.display = False
        for selector in self._FORM_INPUT_IDS:
            field = self.query_one(selector, Input)
            field.display = True
            # Primary connection verification temporarily disables every
            # field. A successful check leaves this screen for the vision
            # step, so reset the state whenever a form is rendered again.
            field.disabled = False
        self.query_one("#setup-provider-help", Static).display = False
        form.refresh(layout=True)
        self._set_actions(next_visible=False, back_visible=False)

        if self._page == "source":
            subtitle.update("步骤 1 / 2：先配置主模型。主模型负责对话、规划、工具调用与数据分析。")
            assert options is not None
            source_options = [
                Option("使用自定义 API Key 或兼容接口", id="custom"),
            ]
            if OFFICIAL_ACCOUNT_UI_ENABLED:
                source_options.append(
                    Option("使用 Aerolytica 官方账号", id="official")
                )
            options.clear_options()
            options.add_options(source_options)
            options.highlighted = 0
            options.focus()
        elif self._page == "official":
            subtitle.update(
                "使用邮箱和密码登录。"
                "密码只用于本次请求，不会保存到本地。"
            )
            form.display = True
            email = self.query_one("#setup-url", Input)
            email.placeholder = "邮箱"
            email.value = ""
            self.query_one("#setup-model", Input).display = False
            password = self.query_one("#setup-key", Input)
            password.placeholder = "密码"
            password.value = ""
            self._set_actions(next_visible=True, back_visible=BYOK_UI_ENABLED)
            self.query_one("#setup-next", Static).update("登录并继续")
            if not BYOK_UI_ENABLED:
                self.query_one("#setup-back", Static).display = False
            email.focus()
        elif self._page == "primary_provider":
            subtitle.update("选择主模型服务商。下一步会列出该服务商支持的模型。")
            assert options is not None
            options.add_options(
                [Option(preset.name, id=provider)
                 for provider, preset in BUILTIN_LLM_PROVIDERS.items()]
                + [Option("其他 OpenAI-compatible 接口", id="custom")]
            )
            self._set_actions(next_visible=False, back_visible=True)
            options.highlighted = 0
            options.focus()
        elif self._page == "primary_model":
            provider = str(self._primary["provider"])
            preset = BUILTIN_LLM_PROVIDERS[provider]
            subtitle.update(f"选择 {preset.name} 主模型。模型可在后续配置中重新切换。")
            assert options is not None
            options.add_options(
                [
                    _model_option(
                        provider,
                        model,
                        recommended=model == preset.default_model,
                    )
                    for model in preset.models
                ]
            )
            self._set_actions(next_visible=False, back_visible=True)
            options.highlighted = 0
            options.focus()
        elif self._page == "primary_form":
            if self._primary_only:
                provider = str(self._primary.get("provider", ""))
                preset = BUILTIN_LLM_PROVIDERS.get(provider)
                title.update(f"配置 {preset.name if preset else provider} API")
                subtitle.update("填写当前供应商的连接信息。API Key 仅保存到用户级 secrets.yaml。")
            else:
                subtitle.update("填写主模型连接信息。API Key 仅保存到用户级 secrets.yaml，不会写入项目配置。")
            form.display = True
            self._configure_primary_form()
            self._show_preset_provider_help(self._primary.get("provider", ""))
            self._set_actions(
                next_visible=True,
                back_visible=not self._primary_only,
                # A primary chat model is required.  Visual and search setup
                # have their own optional choices on subsequent pages.
                skip_visible=False,
            )
            self.query_one("#setup-next", Static).update(
                "保存并切换" if self._primary_only else "继续"
            )
            self._focus_first_visible_field()
        elif self._page == "vision_mode":
            subtitle.update("步骤 2 / 2：视觉模型可选。它用于读图、图表、卫星云图、雷达图和图像型 PDF。")
            items = []
            if self._primary_supports_vision:
                items.append(Option("复用当前模型多模态能力", id="reuse_primary"))
            elif self._can_reuse_bailian_primary_key():
                items.append(
                    Option(
                        "复用主模型已配置的百炼 API Key（选择视觉模型）",
                        id="reuse_bailian_primary_key",
                    )
                )
            items.append(Option("暂不配置，需要时再提示", id="unconfigured"))
            items.append(Option("配置独立视觉模型", id="separate"))
            options.display = True
            options.add_options(items)
            self._set_actions(next_visible=False, back_visible=not self._vision_only, skip_visible=True)
            self.query_one("#setup-skip", Static).update("跳过")
            options.highlighted = 0
            options.focus()
        elif self._page == "vision_model":
            from aero.core.llm_providers import model_supports_vision as _msv
            subtitle.update("选择视觉模型。视觉模型只在需要读取图片时调用。")
            assert options is not None
            for provider_id, preset in BUILTIN_LLM_PROVIDERS.items():
                if self._vision.get("_reuse_bailian_primary_key") and provider_id != "bailian":
                    continue
                for model in preset.models:
                    if _msv(provider_id, model):
                        options.add_option(
                            _vision_option(provider_id, model, preset.name)
                        )
            if not self._vision.get("_reuse_bailian_primary_key"):
                options.add_option(Option("其他 OpenAI-compatible 视觉接口", id="custom"))
            self._set_actions(next_visible=False, back_visible=True)
            options.highlighted = 0
            options.focus()
        elif self._page == "vision_form":
            subtitle.update("填写视觉模型连接信息。保存后，Aero 才会将图片发送给该模型进行分析。")
            form.display = True
            self._configure_vision_form()
            self._show_preset_provider_help(self._vision.get("provider", ""))
            self._set_actions(next_visible=True, back_visible=True)
            self.query_one("#setup-next", Static).update("保存并继续")
            self._focus_first_visible_field()
        elif self._page == "web_search_setup":
            if self._primary.get("provider") == "official":
                self._web_search = {"configured": False}
                self._finish()
                return
            subtitle.update("步骤 3 / 3：联网搜索可选。选择搜索供应商并配置 API Key；百炼还需要先在 MCP 广场开通联网搜索 MCP。")
            assert options is not None
            bailian_source = self._reusable_search_credential("bailian")
            zhipu_source = self._reusable_search_credential("zhipu")
            provider_items = {
                "bailian": (
                    Option(f"复用{bailian_source}已配置的百炼 API Key（推荐）", id="reuse_bailian")
                    if bailian_source
                    else Option("配置百炼（需要开通 WebSearch MCP）", id="configure_bailian")
                ),
                "zhipu": (
                    Option(f"复用{zhipu_source}已配置的智谱 API Key（推荐）", id="reuse_zhipu")
                    if zhipu_source
                    else Option("配置智谱 AI 搜索", id="configure_zhipu")
                ),
            }
            reusable_provider = next(
                (
                    provider
                    for provider, source in (("bailian", bailian_source), ("zhipu", zhipu_source))
                    if source == "主模型"
                ),
                next(
                    (
                        provider
                        for provider, source in (
                            ("bailian", bailian_source),
                            ("zhipu", zhipu_source),
                        )
                        if source
                    ),
                    "",
                ),
            )
            items = (
                [provider_items[reusable_provider], Option("暂不配置，需要时再提示", id="skip")]
                + [item for provider, item in provider_items.items() if provider != reusable_provider]
                if reusable_provider
                else [Option("暂不配置，需要时再提示", id="skip"), *provider_items.values()]
            )
            options.add_options(items)
            self._set_actions(next_visible=False, back_visible=True)
            options.highlighted = 0
            options.focus()
        elif self._page == "web_search_form":
            title.update("配置联网搜索")
            subtitle.update("填写所选搜索供应商的 API Key。接口地址和搜索参数已内置，无需填写。")
            form.display = True
            self.query_one("#setup-url", Input).display = False
            self.query_one("#setup-model", Input).display = False
            self._configure_web_search_form()
            self._set_actions(next_visible=True, back_visible=True)
            self.query_one("#setup-next", Static).update("保存并开始使用")
            self._focus_first_visible_field()

        self.refresh(layout=True)

    def _set_actions(
        self,
        *,
        next_visible: bool,
        back_visible: bool,
        skip_visible: bool = False,
    ) -> None:
        for selector, visible in zip(
            self._ACTION_IDS,
            (next_visible, skip_visible, back_visible, True),
            strict=True,
        ):
            self.query_one(selector, Static).display = visible
        self._action_focused = False
        visible_indices = self._visible_action_indices()
        self._selected_action = visible_indices[0]
        self._sync_selected_action()

    def _visible_action_indices(self) -> list[int]:
        return [
            index
            for index, selector in enumerate(self._ACTION_IDS)
            if self.query_one(selector, Static).display
        ]

    def _current_option_list(self) -> OptionList | None:
        try:
            options = self.query_one("#setup-list", OptionList)
        except Exception:
            return None
        return options if options.display else None

    def _configure_primary_form(self) -> None:
        provider = self._primary.get("provider", "")
        preset = BUILTIN_LLM_PROVIDERS.get(provider)
        self._set_input("url", self._primary.get("base_url", preset.base_url if preset else ""))
        self._set_input("model", self._primary.get("model", preset.default_model if preset else ""))
        self._set_input("key", "")

    def _configure_vision_form(self) -> None:
        provider = self._vision.get("provider", "")
        preset = BUILTIN_LLM_PROVIDERS.get(provider)
        self._set_input("url", self._vision.get("base_url", preset.base_url if preset else ""))
        self._set_input("model", self._vision.get("model", ""))
        self._set_input("key", "")

    def _can_reuse_bailian_primary_key(self) -> bool:
        """Whether a Bailian chat credential can seed a separate vision model."""
        return bool(
            self._primary.get("provider") == "bailian"
            and str(self._primary.get("api_key") or "").strip()
        )

    def _show_preset_provider_help(self, provider: str) -> None:
        """Show the key acquisition help instead of fixed preset fields."""
        preset = BUILTIN_LLM_PROVIDERS.get(provider)
        if preset is None:
            return
        self.query_one("#setup-url", Input).display = False
        self.query_one("#setup-model", Input).display = False
        help_text = self.query_one("#setup-provider-help", Static)
        help_text.update(
            f"获取 {preset.name} API Key：\n"
            f"[link='{preset.api_key_url}']{preset.api_key_url}[/link]\n"
            f"{preset.api_key_hint}"
        )
        help_text.display = True

    def _configure_web_search_form(self) -> None:
        provider = self._web_search.get("provider", "bailian")
        defaults = {
            "bailian": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo"),
            "zhipu": ("https://open.bigmodel.cn/api/paas/v4/web_search", "search_std"),
        }
        key_help = {
            "bailian": (
                "阿里云百炼",
                "https://bailian.console.aliyun.com/cn-beijing/?apiKey=1&tab=globalset#/efm/api_key",
                "创建或复制 DashScope API Key 后，还必须进入百炼 MCP 广场，"
                "搜索“WebSearch”或“联网搜索”，点击“立即开通 → 确认开通”。"
                "API Key 与 MCP 开通两项缺一不可，并请确认账户余额和调用额度可用。"
                "计费：全部用户前 2000 次调用免费，之后按 29 元/千次计费；"
                "价格可能调整，以阿里云官方页面为准。",
            ),
            "zhipu": (
                "智谱 AI",
                "https://open.bigmodel.cn/apikey/platform",
                "在开放平台创建 API Key 后粘贴到下方；无需开通百炼 MCP。"
                "请确认智谱账户余额和搜索调用额度可用。",
            ),
        }
        url, model = defaults.get(provider, defaults["bailian"])
        self._set_input("url", self._web_search.get("base_url", url))
        self._set_input("model", self._web_search.get("model", model))
        self._set_input("key", self._web_search.get("api_key", ""))
        name, key_url, hint = key_help.get(provider, key_help["bailian"])
        help_text = self.query_one("#setup-provider-help", Static)
        help_text.update(
            f"获取 {name} API Key：\n[link='{key_url}']{key_url}[/link]\n{hint}"
        )
        help_text.display = True

    def _reusable_search_credential(self, provider: str) -> str:
        """Return the configured model role that can supply a search API key."""
        vision_key = self._vision.get("api_key", "").strip()
        if (
            self._vision.get("mode") == "separate"
            and self._vision.get("provider") == provider
            and vision_key
        ):
            return "视觉模型"
        primary_key = self._primary.get("api_key", "").strip()
        if self._primary.get("provider") == provider and primary_key:
            return "主模型"
        return ""

    def _search_credential(self, provider: str) -> str:
        """Return the matching configured model key for the selected search provider."""
        if self._reusable_search_credential(provider) == "视觉模型":
            return self._vision.get("api_key", "").strip()
        if self._reusable_search_credential(provider) == "主模型":
            return self._primary.get("api_key", "").strip()
        return ""

    def _set_input(self, name: str, value: str) -> None:
        widget = self.query_one(f"#setup-{name}", Input)
        widget.value = value

    def _visible_input_ids(self) -> list[str]:
        return [selector for selector in self._FORM_INPUT_IDS if self.query_one(selector).display]

    def _focus_first_visible_field(self) -> None:
        self._action_focused = False
        visible = self._visible_input_ids()
        if not visible:
            return
        # API keys are the only value users normally need to type on a
        # pre-filled connection form, so place the cursor there first.
        target = (
            "#setup-key" if self._page != "official" and "#setup-key" in visible else visible[0]
        )
        self.set_focus(self.query_one(target, Input))

    def _move_form_focus(self, delta: int) -> None:
        field_ids = self._visible_input_ids()
        focused = self.focused
        if self._action_focused:
            if delta < 0:
                self._action_focused = False
                self.set_focus(self.query_one(field_ids[-1], Input))
            self._sync_selected_action()
            return
        try:
            index = field_ids.index(f"#{focused.id}") if focused is not None and focused.id else 0
        except ValueError:
            index = 0
        target = index + delta
        if target < 0:
            self.set_focus(self.query_one(field_ids[0], Input))
        elif target >= len(field_ids):
            self._focus_actions()
        else:
            self.set_focus(self.query_one(field_ids[target], Input))

    def _focus_actions(self) -> None:
        visible_indices = self._visible_action_indices()
        if self._selected_action not in visible_indices:
            self._selected_action = visible_indices[0]
        self._action_focused = True
        self.set_focus(None)
        self._sync_selected_action()

    def _move_action_focus(self, delta: int) -> None:
        visible_indices = self._visible_action_indices()
        current = (
            visible_indices.index(self._selected_action)
            if self._selected_action in visible_indices
            else 0
        )
        self._selected_action = visible_indices[(current + delta) % len(visible_indices)]
        self._sync_selected_action()

    def _leave_actions(self) -> None:
        self._action_focused = False
        options = self._current_option_list()
        if options is not None:
            options.focus()
            if options.option_count and options.highlighted is None:
                options.highlighted = options.option_count - 1
        elif self._page in {"official", "primary_form", "vision_form", "web_search_form"}:
            field_ids = self._visible_input_ids()
            self.set_focus(self.query_one(field_ids[-1], Input))
        self._sync_selected_action()

    def _sync_selected_action(self) -> None:
        for index, selector in enumerate(self._ACTION_IDS):
            action = self.query_one(selector, Static)
            action.set_class(
                self._action_focused
                and index == self._selected_action
                and action.display,
                "setup-selected",
            )

    def _activate_selected_action(self) -> None:
        if self._selected_action == 0:
            self._submit_form()
        elif self._selected_action == 1:
            self._skip_vision_setup()
        elif self._selected_action == 2:
            self._go_back()
        else:
            self.dismiss(None)

    def _skip_vision_setup(self) -> None:
        if self._page == "vision_mode":
            self._vision = {"mode": "unconfigured"}
            self._page = "web_search_setup"
            self._render_page()
            return
        if self._page != "primary_form":
            return
        values = self._form_values()
        if not self._validate(values):
            return
        self._primary.update(values)
        self._primary_supports_vision = model_supports_vision(
            values["provider"],
            values["model"],
        )
        self._vision = {"mode": "unconfigured"}
        if self._primary_only:
            self._finish()
            return
        self._page = "web_search_setup"
        self._render_page()

    def _choose(self, choice: str) -> None:
        if self._page == "source":
            self._page = "official" if choice == "official" else "primary_provider"
        elif self._page == "primary_provider":
            self._primary = {"provider": choice}
            self._page = "primary_form" if choice == "custom" else "primary_model"
        elif self._page == "primary_model":
            self._primary["model"] = choice
            self._page = "primary_form"
        elif self._page == "vision_mode":
            if choice == "unconfigured":
                self._vision = {"mode": "unconfigured"}
                self._page = "web_search_setup"
                self._render_page()
                return
            if choice == "reuse_primary":
                self._vision = {"mode": "reuse_primary"}
                self._page = "web_search_setup"
                self._render_page()
                return
            if choice == "reuse_bailian_primary_key":
                preset = BUILTIN_LLM_PROVIDERS["bailian"]
                self._vision = {
                    "mode": "separate",
                    "provider": "bailian",
                    "base_url": str(self._primary.get("base_url") or preset.base_url),
                    "api_key": str(self._primary["api_key"]),
                    "_reuse_bailian_primary_key": True,
                }
                self._page = "vision_model"
                self._render_page()
                return
            self._vision = {"mode": "separate"}
            self._page = "vision_model"
        elif self._page == "vision_model":
            if choice == "custom":
                self._vision["provider"] = ""
                self._vision["model"] = ""
                self._page = "vision_form"
            else:
                provider, model = choice.split("|", 1)
                self._vision["provider"] = provider
                self._vision["model"] = model
                if self._vision.pop("_reuse_bailian_primary_key", False):
                    # The provider key was already entered for the primary
                    # model, so selecting a compatible visual model completes
                    # this optional setup without asking for it again.
                    if self._vision_only:
                        self._finish()
                        return
                    self._page = "web_search_setup"
                    self._render_page()
                    return
                self._page = "vision_form"
        elif self._page == "web_search_setup":
            if choice == "skip":
                self._web_search = {"configured": False}
                self._finish()
                return
            if choice == "reuse_bailian":
                self._web_search = {
                    "configured": True,
                    "provider": "bailian",
                    "api_key": self._search_credential("bailian"),
                }
                self._page = "web_search_form"
                self._render_page()
                self._verifying_web_search = True
                self.run_worker(self._verify_web_search_connection(self._web_search), exclusive=True)
                return
            if choice == "reuse_zhipu":
                self._web_search = {
                    "configured": True,
                    "provider": "zhipu",
                    "api_key": self._search_credential("zhipu"),
                }
                self._page = "web_search_form"
                self._render_page()
                self._verifying_web_search = True
                self.run_worker(self._verify_web_search_connection(self._web_search), exclusive=True)
                return
            provider = "bailian" if choice == "configure_bailian" else choice.removeprefix("configure_")
            self._web_search = {"configured": True, "provider": provider}
            self._page = "web_search_form"
        self._render_page()

    def _submit_form(self) -> None:
        if self._page == "official":
            if self._verifying_primary:
                return
            email = self.query_one("#setup-url", Input).value.strip()
            password = self.query_one("#setup-key", Input).value
            if not email or not password:
                self.query_one("#setup-error", Static).update("请输入邮箱和密码。")
                return
            self._verifying_primary = True
            status = self.query_one("#setup-error", Static)
            status.remove_class("setup-success")
            status.add_class("setup-progress")
            status.update("正在登录官方账户…")
            self._set_actions(next_visible=False, back_visible=False)
            self.run_worker(self._verify_official_login(email, password), exclusive=True)
            return
        if self._page == "primary_form":
            if self._verifying_primary:
                return
            values = self._form_values()
            if not self._validate(values):
                return
            self._verifying_primary = True
            status = self.query_one("#setup-error", Static)
            status.remove_class("setup-success", "setup-progress")
            status.add_class("setup-progress")
            status.update("正在测试模型连接，请稍候…")
            for selector in self._FORM_INPUT_IDS:
                self.query_one(selector, Input).disabled = True
            self._set_actions(next_visible=False, back_visible=False)
            self.run_worker(self._verify_primary_connection(values), exclusive=True)
            return
        if self._page == "vision_form":
            if self._verifying_vision:
                return
            values = self._form_values()
            if not self._validate(values):
                return
            self._verifying_vision = True
            status = self.query_one("#setup-error", Static)
            status.remove_class("setup-success", "setup-progress")
            status.add_class("setup-progress")
            status.update("正在测试视觉模型连接，请稍候…")
            for selector in self._FORM_INPUT_IDS:
                self.query_one(selector, Input).disabled = True
            self._set_actions(next_visible=False, back_visible=False)
            self.run_worker(self._verify_vision_connection(values), exclusive=True)
            return
        if self._page == "web_search_form":
            values = self._form_values()
            required = (("API Key", "api_key"),)
            missing = [label for label, key in required if not values[key]]
            if missing:
                self.query_one("#setup-error", Static).update("请填写：" + "、".join(missing))
                return
            if self._verifying_web_search:
                return
            self._verifying_web_search = True
            self._web_search.update(values)
            status = self.query_one("#setup-error", Static)
            status.remove_class("setup-success", "setup-progress")
            status.add_class("setup-progress")
            status.update("正在测试网页搜索连接，请稍候…")
            self.query_one("#setup-key", Input).disabled = True
            self._set_actions(next_visible=False, back_visible=False)
            self.run_worker(self._verify_web_search_connection(values), exclusive=True)

    async def _verify_official_login(self, email: str, password: str) -> None:
        from aero.core.official_account import (
            OfficialAccountError,
            OfficialAccountSession,
            relay_llm_url,
        )

        session = OfficialAccountSession()
        try:
            await session.login(email, password)
        except OfficialAccountError as exc:
            self._verifying_primary = False
            self._set_actions(next_visible=True, back_visible=BYOK_UI_ENABLED)
            self.query_one("#setup-next", Static).update("登录并继续")
            status = self.query_one("#setup-error", Static)
            status.remove_class("setup-progress")
            status.update(str(exc))
            self.query_one("#setup-key", Input).value = ""
            self.query_one("#setup-key", Input).focus()
            return
        finally:
            await session.close()
        self._verifying_primary = False
        self._primary = {
            "provider": "official",
            "model": "auto",
            "base_url": relay_llm_url(),
            "api_key": "",
        }
        self._primary_supports_vision = False
        if not BYOK_UI_ENABLED:
            self._vision = {"mode": "official"}
            self._finish()
            return
        self._page = "vision_mode"
        self._render_page()

    async def _verify_primary_connection(self, values: dict[str, str]) -> None:
        """Block setup until the chosen endpoint accepts its API key and model."""
        try:
            await _test_primary_connection(values)
        except Exception as exc:
            self._verifying_primary = False
            for selector in self._FORM_INPUT_IDS:
                self.query_one(selector, Input).disabled = False
            self._set_actions(
                next_visible=True,
                back_visible=not self._primary_only,
                skip_visible=False,
            )
            detail = str(exc).replace("\n", " ").strip()
            if len(detail) > 240:
                detail = detail[:237] + "…"
            status = self.query_one("#setup-error", Static)
            status.remove_class("setup-success", "setup-progress")
            status.update(
                f"连通性测试失败：{detail or '无法连接模型服务。'} 请检查 API Key、接口地址和模型 ID。"
            )
            self._focus_first_visible_field()
            return

        status = self.query_one("#setup-error", Static)
        status.remove_class("setup-progress")
        status.add_class("setup-success")
        status.update("测试通过")
        await asyncio.sleep(self._CONNECTION_SUCCESS_DELAY)

        self._verifying_primary = False
        self._primary.update(values)
        self._primary_supports_vision = model_supports_vision(
            values["provider"], values["model"]
        )
        if self._primary_only:
            self._vision = {"mode": "unconfigured"}
            self._finish()
            return
        self._page = "vision_mode"
        self._render_page()

    async def _verify_vision_connection(self, values: dict[str, str]) -> None:
        """Verify a visual endpoint without sending an image, then continue."""
        try:
            await _test_primary_connection(values)
        except Exception as exc:
            self._verifying_vision = False
            for selector in self._FORM_INPUT_IDS:
                self.query_one(selector, Input).disabled = False
            self._set_actions(next_visible=True, back_visible=True)
            detail = str(exc).replace("\n", " ").strip()
            if len(detail) > 240:
                detail = detail[:237] + "…"
            status = self.query_one("#setup-error", Static)
            status.remove_class("setup-success", "setup-progress")
            status.update(
                f"连通性测试失败：{detail or '无法连接视觉模型服务。'} 请检查 API Key、接口地址和模型 ID。"
            )
            self._focus_first_visible_field()
            return

        status = self.query_one("#setup-error", Static)
        status.remove_class("setup-progress")
        status.add_class("setup-success")
        status.update("测试通过")
        await asyncio.sleep(self._CONNECTION_SUCCESS_DELAY)

        self._verifying_vision = False
        self._vision.update(values)
        if self._vision_only:
            self._finish()
            return
        self._page = "web_search_setup"
        self._render_page()

    async def _verify_web_search_connection(self, values: dict[str, str]) -> None:
        """Verify the selected MCP/direct search service before setup completes."""
        try:
            from aero.data.web_search import check_bailian_web, search_zhipu_web

            if values["provider"] == "bailian":
                await check_bailian_web(values["api_key"])
            elif values["provider"] == "zhipu":
                await search_zhipu_web(values["api_key"], "联网搜索连通性测试", limit=1)
            else:
                raise RuntimeError("不支持的网页搜索供应商")
        except Exception as exc:
            self._verifying_web_search = False
            self.query_one("#setup-key", Input).disabled = False
            self._set_actions(next_visible=True, back_visible=True)
            detail = str(exc).replace("\n", " ").strip()[:240]
            status = self.query_one("#setup-error", Static)
            status.remove_class("setup-success", "setup-progress")
            status.update(f"网页搜索连通性测试失败：{detail or '服务不可用'}")
            self._focus_first_visible_field()
            return
        status = self.query_one("#setup-error", Static)
        status.remove_class("setup-progress")
        status.add_class("setup-success")
        status.update("测试通过")
        await asyncio.sleep(self._CONNECTION_SUCCESS_DELAY)
        self._verifying_web_search = False
        self._web_search.update(values)
        self._web_search["mcp_verified"] = True
        self._finish()

    def _form_values(self) -> dict[str, str]:
        if self._page == "web_search_form":
            return {
                "provider": self._web_search.get("provider", "bailian"),
                "label": self._web_search.get("provider", "bailian"),
                "base_url": self.query_one("#setup-url", Input).value.strip(),
                "model": self.query_one("#setup-model", Input).value.strip(),
                "api_key": self.query_one("#setup-key", Input).value.strip(),
            }
        selected = self._primary if self._page == "primary_form" else self._vision
        provider_id = str(selected.get("provider", ""))
        return {
            "provider": provider_id,
            "label": provider_id,
            "base_url": self.query_one("#setup-url", Input).value.strip(),
            "model": self.query_one("#setup-model", Input).value.strip(),
            "api_key": self.query_one("#setup-key", Input).value.strip(),
        }

    def _validate(self, values: dict[str, str]) -> bool:
        required = (("服务商", "provider"), ("接口地址", "base_url"), ("模型 ID", "model"), ("API Key", "api_key"))
        missing = [label for label, key in required if not values[key]]
        if missing:
            self.query_one("#setup-error", Static).update("请填写：" + "、".join(missing))
            return False
        if not values["base_url"].startswith(("https://", "http://")):
            self.query_one("#setup-error", Static).update("接口地址必须以 http:// 或 https:// 开头。")
            return False
        return True

    def _go_back(self) -> None:
        previous = {
            "official": "source",
            "primary_provider": "source",
            "primary_model": "primary_provider",
            "primary_form": (
                "primary_provider"
                if self._primary.get("provider") == "custom"
                else "primary_model"
            ),
            "vision_mode": "primary_form",
            "vision_model": "vision_mode",
            "vision_form": "vision_model",
            "web_search_setup": (
                "vision_form"
                if self._vision.get("mode") == "separate"
                else "vision_mode"
            ),
            "web_search_form": "web_search_setup",
        }
        self._page = previous.get(self._page, "source")
        self._render_page()
        if self._page in {"primary_form", "vision_form", "web_search_form"}:
            # Textual completes focus cleanup for the formerly focused
            # OptionList after this handler returns. Focus once more after
            # that cleanup has run, so returning to a form is deterministic.
            self.set_timer(0.05, self._focus_first_visible_field)

    def _finish(self) -> None:
        if self._vision_only:
            self.dismiss({"vision": self._vision})
            return
        self.dismiss({
            "primary": self._primary,
            "primary_supports_vision": self._primary_supports_vision,
            "vision": self._vision,
            "web_search": self._web_search,
        })


async def _test_primary_connection(values: dict[str, str]) -> None:
    """Perform the smallest real completion request accepted by the chosen API.

    A successful response verifies the endpoint, API key and selected model
    together.  `max_tokens=1` keeps this setup-time check negligible in cost.
    """
    from aero.agent.llm_client import LLMClient, LLMConfig

    client = LLMClient(
        LLMConfig(
            provider=values["provider"],
            model=values["model"],
            api_key=values["api_key"],
            base_url=values["base_url"],
            max_tokens=1,
        )
    )
    try:
        await client.chat([Message(role="user", content="Reply with OK.")])
    finally:
        await client.close()
