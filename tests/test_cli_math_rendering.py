from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from aero.cli.main import (
    AeroApp,
    ChatMarkdown,
    ConfirmScreen,
    _assistant_claims_background_handoff,
    _billing_markdown,
    _checkpoint_title_from_messages,
    _checkpoint_title_prompt,
    _command_requires_input,
    _command_suggestions,
    _compacted_context_messages,
    _estimate_context_tokens,
    _format_checkpoint_list_entry,
    _has_persistable_session_messages,
    _help_text,
    _is_billing_query,
    _is_subagent_tool_status,
    _load_saved_theme,
    _normalize_checkpoint_title,
    _normalize_confirm_choice,
    _normalize_generated_session_title,
    _render_status_lines,
    _render_terminal_math,
    _resolve_theme_name,
    _restore_confirmation_message,
    _save_user_theme,
    _session_option_label,
    _session_title_from_messages,
    _session_title_prompt,
    _should_queue_input_during_run,
    _status_progress_slot,
    _theme_options,
    _usage_meta_text,
)
from aero.core.config import AeroConfig
from aero.core.types import Message, ToolCall
from aero.data import pricing
from aero.data.pricing import ModelPrice, ModelUsage, TokenTracker
from aero.i18n import t


def test_render_terminal_math_removes_display_latex_markers():
    text = r"$$ DSWRF = \text{直接短波辐射} + \text{散射短波辐射} $$"

    rendered = _render_terminal_math(text)

    assert rendered == "DSWRF = 直接短波辐射 + 散射短波辐射"
    assert "$$" not in rendered
    assert r"\text" not in rendered


def test_gcs_heartbeat_status_replaces_previous_line():
    first = "GCS ARCO 正在隔离进程中读取远程 Zarr 并写入 NetCDF，已等待 5s ..."
    second = "GCS ARCO 正在隔离进程中读取远程 Zarr 并写入 NetCDF，已等待 10s ..."

    assert _status_progress_slot(first) == _status_progress_slot(second)


def test_render_terminal_math_handles_inline_units_and_symbols():
    text = r"单位是 $W/m^2$，波长约 $0.2\text{-}4\ \mu m$。"

    rendered = _render_terminal_math(text)

    assert "W/m²" in rendered
    assert "0.2-4 μ m" in rendered
    assert "$" not in rendered


def test_render_terminal_math_converts_fraction():
    text = r"$\frac{a}{b} \approx c$"

    rendered = _render_terminal_math(text)

    assert rendered == "(a)/(b) ≈ c"


def test_render_terminal_math_handles_parenthesized_latex():
    text = r"其中：\(\tau\) 是透射率，\(S_0\) 是太阳常数，\(\theta_z\) 是天顶角。"

    rendered = _render_terminal_math(text)

    assert "τ 是透射率" in rendered
    assert "S₀ 是太阳常数" in rendered
    assert "θ_z 是天顶角" in rendered
    assert r"\tau" not in rendered
    assert r"\theta" not in rendered


def test_render_terminal_math_handles_integral_formula():
    text = (
        r"\["
        r"DSWRF = \int_{0.2}^{4} \int_{0}^{\pi} I_\lambda(\theta,\phi)"
        r"\cos\theta \sin\theta\, d\theta\, d\phi\, d\lambda"
        r"\]"
    )

    rendered = _render_terminal_math(text)

    assert "DSWRF = ∫_0.2^4 ∫₀^π I_λ(θ,φ)cosθ sinθ dθ dφ dλ" == rendered
    assert "\\" not in rendered


def test_render_terminal_math_handles_swh_sqrt_integral_formula():
    text = r"$H_s = 4 \sqrt{\int\int S(f,\theta)\,df\,d\theta}$"

    rendered = _render_terminal_math(text)

    assert rendered == "Hₛ = 4 √(∫∫ S(f,θ) df dθ)"
    assert "sqrt" not in rendered


def test_render_terminal_math_repairs_plain_sqrtint():
    text = "Hₛ = 4 sqrtint S(f,θ) df dθ"

    rendered = _render_terminal_math(text)

    assert rendered == "Hₛ = 4 √∫ S(f,θ) df dθ"


def test_chat_markdown_renders_cjk_adjacent_strong_emphasis():
    source = "一天中**午后（14:00）**气温最高，**凌晨（02:00）**最低。"

    rendered = _render_terminal_math(source)
    [paragraph] = ChatMarkdown()._build_from_source(rendered)

    assert "**" not in paragraph._content.plain
    assert [span.style for span in paragraph._content.spans] == [".strong", ".strong"]
    assert paragraph._content.plain == ("一天中 午后（14:00） 气温最高， 凌晨（02:00） 最低。")


def test_cjk_emphasis_normalization_leaves_inline_and_fenced_code_unchanged():
    source = "正文中**重点**继续，`代码中**原样**保留`\n```\n中文**原样**\n```\n"

    rendered = _render_terminal_math(source)

    assert "正文中 **重点** 继续" in rendered
    assert "`代码中**原样**保留`" in rendered
    assert "```\n中文**原样**\n```" in rendered


def test_render_status_lines_places_activity_before_last_line():
    rendered = _render_status_lines(
        ["正在配置视觉模型", "视觉模型配置完成", "正在调用视觉模型分析图片"],
        activity="⠋",
    )

    assert rendered.splitlines() == [
        "  正在配置视觉模型",
        "  视觉模型配置完成",
        "⠋ 正在调用视觉模型分析图片",
    ]


def test_runtime_tool_confirmation_message_is_human_readable():
    app = AeroApp.__new__(AeroApp)
    app.config = AeroConfig.create_default()

    message = AeroApp._build_confirm_message(
        app,
        "ensure_runtime_tools",
        {"tools": ["cdo", "grib_to_netcdf"]},
    )

    assert "安装命令行工具" in message
    assert "cdo、grib_to_netcdf" in message
    assert "aero-agent" in message
    assert "mamba" in message
    assert "conda-forge" in message
    assert "参数" not in message
    assert '"tools"' not in message


def test_runtime_setup_shell_batch_confirmation_shows_raw_commands_without_json():
    app = AeroApp.__new__(AeroApp)
    app.config = AeroConfig.create_default()

    message = AeroApp._build_confirm_message(
        app,
        "run_shell",
        {},
        [
            {
                "command": "mkdir -p ~/.aero/runtime/envs/aero-agent",
                "description": "创建 Aero 运行环境目录",
            },
            {
                "command": "ln -sfn ~/.aero/runtime/envs/aero-agent/bin ~/.aero/runtime/bin",
                "description": "创建 Aero 运行环境链接",
            },
        ],
    )

    assert "初始化 Aero 私有运行环境" in message
    assert "命令（2 条）" in message
    assert "当前项目文件" in message
    assert "aero runtime clean" in message
    assert "mkdir -p ~/.aero/runtime/envs/aero-agent" in message
    assert "ln -sfn ~/.aero/runtime/envs/aero-agent/bin ~/.aero/runtime/bin" in message
    assert "参数" not in message
    assert '"command"' not in message


def test_render_status_lines_omits_activity_when_stopped():
    rendered = _render_status_lines(["继续执行...", "正在调用视觉模型分析图片"])

    assert rendered.splitlines() == [
        "  继续执行...",
        "  正在调用视觉模型分析图片",
    ]


def test_resolve_theme_name_supports_shortcuts():
    themes = {"textual-dark": object(), "textual-light": object(), "dracula": object()}

    assert _resolve_theme_name("dark", themes) == "textual-dark"
    assert _resolve_theme_name("light", themes) == "textual-light"
    assert _resolve_theme_name("dracula", themes) == "dracula"
    assert _resolve_theme_name("missing", themes) is None


def test_theme_options_use_readable_labels():
    themes = {"textual-dark": object(), "catppuccin-mocha": object()}

    assert _theme_options(themes) == [
        ("textual-dark", "Textual Dark"),
        ("catppuccin-mocha", "Catppuccin Mocha"),
    ]


def test_usage_meta_text_formats_cache_hit_as_status_segment():
    tracker = TokenTracker(
        _llm_usage={
            "deepseek-v4-flash": ModelUsage(
                prompt_tokens=1000, completion_tokens=100, cached_tokens=950
            )
        },
        current_prompt_tokens=43_300,
    )

    text = _usage_meta_text(tracker, "deepseek-v4-flash")

    assert "[dim]上下文[/dim] 43.3K [dim]/ 4%[/dim]" in text
    assert "[dim]命中缓存[/dim] 95%" in text
    assert "[dim]会话累计[/dim] ¥" in text
    assert "¥" in text
    assert "[dim]cost[/dim]" not in text
    assert "♻" not in text
    assert "(4%)" not in text


def test_usage_meta_text_can_hide_cost_for_official_provider():
    tracker = TokenTracker(
        _llm_usage={
            "deepseek-v4-flash": ModelUsage(
                prompt_tokens=1000, completion_tokens=100, cached_tokens=0
            )
        },
        current_prompt_tokens=1100,
    )

    text = _usage_meta_text(
        tracker,
        "deepseek-v4-flash",
        include_cost=False,
    )

    assert "[dim]上下文[/dim]" in text
    assert "会话累计" not in text
    assert "¥" not in text


def test_qwen37_plus_pricing_applies_bailian_cache_discount():
    tracker = TokenTracker()
    tracker.add_llm(
        {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 1_000_000,
            "prompt_tokens_details": {"cached_tokens": 800_000},
        },
        "qwen3.7-plus",
    )

    # Standard China-mainland price: input ¥2/M, cached input 20%, output ¥8/M.
    assert tracker.total_cost() == 8.72


def test_session_option_label_uses_iso_time_without_usage():
    text = _session_option_label("本地数据文件清单与内容概览", 1780730520)

    assert text.startswith("本地数据文件清单与内容概览 (2026-")
    assert "T" in text
    assert "93.2K" not in text
    assert "¥" not in text


def test_command_suggestions_include_set_subcommands_after_space():
    primary = [
        ("/set", "设置参数"),
        ("/session", "历史会话"),
        ("/checkpoint", "创建检查点"),
        ("/experiment", "开始新的实验分支"),
        ("/checkpoints", "检查点"),
    ]
    secondary = [
        ("/set max_tool_rounds ", "设置最大工具调用轮次"),
        ("/session rename ", "修改当前会话标题"),
        ("/checkpoint rename ", "修改检查点名称"),
        ("/checkpoints all", "显示全部检查点"),
        ("/checkpoints clear", "清理全部检查点"),
        ("/experiment ", "从当前状态开始实验分支"),
    ]

    assert _command_suggestions("/", primary, secondary) == primary
    assert _command_suggestions("/set ", primary, secondary) == [
        ("/set max_tool_rounds ", "设置最大工具调用轮次")
    ]
    assert _command_suggestions("/set max", primary, secondary) == [
        ("/set max_tool_rounds ", "设置最大工具调用轮次")
    ]
    assert _command_suggestions("/checkpoints", primary, secondary) == [
        ("/checkpoints", "检查点"),
        ("/checkpoints all", "显示全部检查点"),
        ("/checkpoints clear", "清理全部检查点"),
    ]
    assert _command_suggestions("/session", primary, secondary) == [
        ("/session", "历史会话"),
        ("/session rename ", "修改当前会话标题"),
    ]
    assert _command_suggestions("/checkpoint ren", primary, secondary) == [
        ("/checkpoint rename ", "修改检查点名称")
    ]
    assert _command_suggestions("/exp", primary, secondary) == [("/experiment", "开始新的实验分支")]


def test_commands_with_required_followup_are_completed_before_execution():
    assert _command_requires_input("/set") is True
    assert _command_requires_input("/restore") is True
    assert _command_requires_input("/experiment") is True
    assert _command_requires_input("/paper") is True
    assert _command_requires_input("/checkpoints") is False
    assert _command_requires_input("/checkpoints all") is False
    assert _command_requires_input("/checkpoint") is False


def test_normalize_confirm_choice_preserves_allow_and_always():
    assert _normalize_confirm_choice("allow") == "allow"
    assert _normalize_confirm_choice("approve") == "approve"
    assert _normalize_confirm_choice("always") == "always"
    assert _normalize_confirm_choice("deny") == "deny"
    assert _normalize_confirm_choice("defer") == "deny"
    assert _normalize_confirm_choice("unexpected") == "deny"


def test_operation_confirmation_hides_session_wide_permission():
    screen = ConfirmScreen(
        "restore details",
        "zh",
        title="恢复检查点",
        allow_label="确认恢复",
        deny_label="取消",
        show_always=False,
    )

    assert screen._title == "恢复检查点"
    assert screen._allow_label == "确认恢复"
    assert screen._deny_label == "取消"
    assert screen._button_ids == ("#btn-allow", "#btn-deny")


@pytest.mark.asyncio
async def test_confirm_scroll_keys_do_not_reach_background_chat():
    class ConfirmHost(App):
        def __init__(self):
            super().__init__()
            self.background_key_events = 0

        def compose(self) -> ComposeResult:
            with VerticalScroll(id="background-chat"):
                for index in range(80):
                    yield Static(f"background line {index}")

        def on_key(self, event: events.Key) -> None:
            if event.key == "up":
                self.background_key_events += 1
                self.query_one("#background-chat", VerticalScroll).scroll_up(animate=False)

    app = ConfirmHost()
    async with app.run_test(size=(100, 30)) as pilot:
        background = app.query_one("#background-chat", VerticalScroll)
        background.scroll_end(animate=False)
        await pilot.pause()
        background_before = background.scroll_y

        screen = ConfirmScreen("\n".join(f"memo line {i}" for i in range(80)))
        await app.push_screen(screen)
        message_box = screen.query_one("#confirm-message-box", VerticalScroll)
        message_box.scroll_end(animate=False)
        await pilot.pause()
        message_before = message_box.scroll_y

        await pilot.press("up")
        await pilot.pause()

        assert app.background_key_events == 0
        assert background.scroll_y == background_before
        assert message_box.scroll_y < message_before


def test_restore_confirmation_uses_user_facing_file_actions():
    diff = type(
        "Diff",
        (),
        {
            "modified": ("scripts/plot.py",),
            "missing": (),
            "added": ("aero.yaml",),
            "references_changed": ("data/sample.nc",),
        },
    )()

    text = _restore_confirmation_message({"name": "基线"}, diff)

    assert "恢复到「基线」后" in text
    assert "覆盖当前修改（1 个）" in text
    assert "scripts/plot.py" in text
    assert "删除此后新增文件（1 个）" in text
    assert "aero.yaml" in text
    assert "数据文件与当时不同，内容将保持不变" in text
    assert "受控文件" not in text
    assert "恢复保护记录" in text


def test_should_queue_input_only_while_model_is_replying():
    state = type("State", (), {})()

    assert _should_queue_input_during_run(None) is True

    state.phase = "thinking"
    assert _should_queue_input_during_run(state) is True

    state.phase = "text"
    assert _should_queue_input_during_run(state) is True

    state.phase = "tool"
    assert _should_queue_input_during_run(state) is False


def test_detects_assistant_background_handoff_claims():
    assert _assistant_claims_background_handoff("好的，那我把这个任务交给后台处理。")
    assert _assistant_claims_background_handoff("已经转交后台任务，完成后通知你。")
    assert not _assistant_claims_background_handoff("这个任务无法转交后台。")
    assert not _assistant_claims_background_handoff("我会在当前对话里继续处理。")


def test_subagent_tool_status_is_not_auto_handoff_trigger():
    assert _is_subagent_tool_status("正在转交后台任务")
    assert _is_subagent_tool_status("后台任务已启动")
    assert not _is_subagent_tool_status("准备下载数据")


def test_set_max_tool_rounds_reports_in_footer():
    app = AeroApp.__new__(AeroApp)
    app.config = AeroConfig.create_default()
    app.agent = type("Agent", (), {"max_tool_rounds": 20})()
    app.last_error = ""
    messages = []
    app._set_footer_status = lambda message: messages.append(message)

    AeroApp._handle_set_command(app, "/set max_tool_rounds 100")

    assert app.agent.max_tool_rounds == 100
    assert messages == ["max_tool_rounds 已设置为 100"]


def test_estimate_context_tokens_uses_token_like_scale():
    messages = [
        Message(role="system", content="s" * 30),
        Message(role="user", content="u" * 60),
    ]

    assert _estimate_context_tokens(messages) == 34


def test_estimate_context_tokens_handles_tool_calls():
    messages = [
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="analyze_image",
                    arguments={"image_paths": ["figures/a.png"]},
                )
            ],
        )
    ]

    assert _estimate_context_tokens(messages) > 0


def test_compacted_context_messages_keep_only_summary_context():
    messages = _compacted_context_messages(
        Message(role="system", content="system"),
        "用户要分析 ERA5 图；已生成 figures/a.png。",
    )

    assert [m.role for m in messages] == ["system", "user", "assistant"]
    assert "compact_summary" in messages[1].content
    assert "figures/a.png" in messages[1].content
    assert all(m.role != "tool" for m in messages)


def test_compact_running_text_is_translated():
    assert t("app.compact_running", "zh") == "正在压缩上下文"


def test_vision_cost_uses_cached_input_price(monkeypatch):
    monkeypatch.setitem(
        pricing.PRICING,
        "test-vision-cache",
        ModelPrice(
            input_price=10,
            cached_input_price=1,
            output_price=20,
            context_window=32_768,
        ),
    )
    tracker = TokenTracker()

    tracker.add_vision(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 800},
        },
        "test-vision-cache",
    )

    assert tracker.vision_cached_tokens == 800
    assert tracker.total_cost() == ((200 * 10) + (800 * 1) + (100 * 20)) / 1000


def test_token_tracker_from_dict_defaults_missing_cached_tokens():
    tracker = TokenTracker.from_dict(
        {
            "llm_usage": {"test": {"prompt_tokens": 1, "completion_tokens": 2}},
            "vision_usage": {},
            "current_prompt_tokens": 0,
        }
    )

    assert tracker.cached_tokens == 0
    assert tracker.to_dict()["llm_usage"]["test"]["cached_tokens"] == 0


def test_token_tracker_from_dict_roundtrip_preserves_per_model_usage():
    original = TokenTracker()
    original.add_llm(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 30},
        },
        "deepseek-v4-pro",
    )
    original.add_llm(
        {"prompt_tokens": 200, "completion_tokens": 80},
        "kimi-k2.6",
    )

    restored = TokenTracker.from_dict(original.to_dict())

    assert restored.prompt_tokens == 300
    assert restored.completion_tokens == 130
    assert restored.cached_tokens == 30
    assert restored.total_cost() == original.total_cost()


def test_token_tracker_includes_and_persists_per_call_services():
    tracker = TokenTracker()
    tracker.add_service("web_search:bailian", calls=2, unit_price=0.029)
    tracker.add_service("web_search:zhipu", unit_price=0.01)

    restored = TokenTracker.from_dict(tracker.to_dict())

    assert restored.service_cost == 0.068
    assert restored.total_cost() == 0.068
    assert restored.copy().to_dict()["service_usage"] == tracker.to_dict()["service_usage"]


def test_service_cost_keeps_historical_price_when_rate_changes():
    tracker = TokenTracker()
    tracker.add_service("search", unit_price=0.01)
    tracker.add_service("search", unit_price=0.02)

    assert tracker.service_cost == 0.03


def test_billing_markdown_lists_models_services_amounts_and_shares(monkeypatch):
    monkeypatch.setitem(
        pricing.PRICING,
        "test-bill-model",
        ModelPrice(
            input_price=1,
            cached_input_price=0.5,
            output_price=2,
            context_window=10_000,
        ),
    )
    tracker = TokenTracker()
    tracker.add_llm(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "prompt_tokens_details": {"cached_tokens": 200},
        },
        "test-bill-model",
    )
    tracker.add_service("web_search:zhipu", calls=10, unit_price=0.01)

    bill = _billing_markdown(tracker, "zh")

    assert "## 会话账单" in bill
    assert "test-bill-model" in bill
    assert "智谱 search_std" in bill
    assert "¥1.90" in bill
    assert "¥0.100" in bill
    assert "95.0%" in bill
    assert "5.0%" in bill
    assert "**总计：¥2.00**" in bill


def test_billing_natural_language_intent_is_explicit_and_local():
    assert _is_billing_query("查看本会话账单")
    assert _is_billing_query("各模型花费占比是多少？")
    assert _is_billing_query("钱都花在哪里了")
    assert _is_billing_query("Show session bill")
    assert not _is_billing_query("如何降低模型推理成本？")
    assert not _is_billing_query("帮我分析这份财务账单中的异常交易")


def test_deepseek_official_peak_pricing_is_applied_at_call_time():
    tracker = TokenTracker()
    usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    beijing = ZoneInfo("Asia/Shanghai")

    tracker.add_llm(
        usage,
        "deepseek-v4-flash",
        provider="deepseek",
        occurred_at=datetime(2026, 7, 26, 8, 59, tzinfo=beijing),
    )
    tracker.add_llm(
        usage,
        "deepseek-v4-flash",
        provider="deepseek",
        occurred_at=datetime(2026, 7, 26, 9, 0, tzinfo=beijing),
    )

    assert tracker.total_cost() == pytest.approx(0.009)


def test_bailian_deepseek_uses_bailian_list_price_without_peak_multiplier():
    tracker = TokenTracker()
    usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    peak = datetime(2026, 7, 26, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    tracker.add_llm(
        usage,
        "deepseek-v4-pro",
        provider="bailian",
        occurred_at=peak,
    )

    assert tracker.total_cost() == pytest.approx(0.036)


def test_deepseek_official_cache_hit_field_uses_cache_price():
    tracker = TokenTracker()
    tracker.add_llm(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 0,
            "prompt_cache_hit_tokens": 800,
        },
        "deepseek-v4-flash",
        provider="deepseek",
        occurred_at=datetime(
            2026,
            7,
            26,
            8,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
    )

    assert tracker.cached_tokens == 800
    assert tracker.total_cost() == pytest.approx(0.000216)


def test_exit_session_save_is_idempotent():
    app = AeroApp.__new__(AeroApp)
    app._session_saved_on_exit = False
    calls = []
    app._auto_save_session = lambda: calls.append("saved")

    AeroApp._save_session_on_exit(app)
    AeroApp._save_session_on_exit(app)

    assert calls == ["saved"]


def test_auto_save_ignores_display_only_local_interactions():
    app = AeroApp.__new__(AeroApp)
    app.agent = SimpleNamespace(messages=[Message(role="system", content="system")])
    app._chat_transcript = lambda: [
        {"role": "user", "content": "sk-a...1234"},
        {"role": "assistant", "content": "API Key 已保存"},
    ]
    app._get_experiment_mgr = lambda: pytest.fail("local interaction must not be saved")

    AeroApp._auto_save_session(app)


@pytest.mark.parametrize("continue_flag", ["--continue", "-c"])
def test_chat_continue_flag_resumes_last_tui_session(monkeypatch, tmp_path, continue_flag):
    import importlib
    import sys

    cli_main = importlib.import_module("aero.cli.main")
    captured = {}

    class FakeApp:
        def __init__(self, config, **kwargs):
            captured.update(kwargs)

        def run(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(sys, "argv", ["aero", "chat", continue_flag])
    monkeypatch.setattr(cli_main, "configure_debug_logging", lambda: Path(tmp_path) / "debug.log")
    monkeypatch.setattr(cli_main, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(cli_main, "_load_config", lambda: SimpleNamespace())
    monkeypatch.setattr(cli_main, "_config_needs_llm_setup", lambda _config: False)
    monkeypatch.setattr(cli_main, "AeroApp", FakeApp)

    cli_main.main()

    assert captured["resume_last_session"] is True
    assert captured["mouse"] is True


@pytest.mark.parametrize("removed_flag", ["--simple", "--no-tui"])
def test_removed_non_tui_flags_fail_explicitly(monkeypatch, tmp_path, capsys, removed_flag):
    import importlib
    import sys

    cli_main = importlib.import_module("aero.cli.main")
    monkeypatch.setattr(sys, "argv", ["aero", "chat", removed_flag])
    monkeypatch.setattr(cli_main, "configure_debug_logging", lambda: Path(tmp_path) / "debug.log")
    monkeypatch.setattr(cli_main, "configure_logging", lambda **_kwargs: None)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main()

    assert exc_info.value.code == 2
    assert "已移除" in capsys.readouterr().out


def test_cli_usage_only_advertises_tui_chat(capsys):
    from aero.cli.main import _print_usage

    _print_usage()
    output = capsys.readouterr().out

    assert "aero chat --continue" in output
    assert "--simple" not in output
    assert "--no-tui" not in output


def test_help_text_includes_current_commands(byok_ui):
    text = _help_text("zh")

    assert "/vision" in text
    assert "/mode" in text
    assert "/new [标题]" in text
    assert "/session" in text
    assert "/session rename" in text
    assert "/checkpoint" in text
    assert "/checkpoint rename" in text
    assert "/checkpoints" in text
    assert "/checkpoints clear" in text
    assert "/restore" in text
    assert "/experiment" in text
    assert "/experiment finish" in text
    assert "/experiment delete" in text
    assert "/experiments" in text
    assert "/experiments clear" in text
    assert "all" in text
    assert "Backspace/Delete 删除" in text
    assert "/compact" in text
    assert "缓存命中" in text


def test_restore_protection_checkpoint_list_item_has_distinct_style():
    markdown = ChatMarkdown()
    [checkpoint_list] = markdown._build_from_source(
        "- `manual` 普通检查点\n- **恢复前安全检查点：基线** · 2026-07-16 22:20"
    )
    regular, safety = list(checkpoint_list.compose())

    assert not regular.has_class("checkpoint-safety")
    assert safety.has_class("checkpoint-safety")


def test_checkpoint_list_entry_only_shows_name_time_and_id():
    text = _format_checkpoint_list_entry(
        {
            "id": "20260716-212758-81c7a82b",
            "name": "第二版",
            "created_at": 1784218078,
            "experiment": "restore-20260716",
            "files": [{"path": "data/input.nc", "restore": "reference"}],
        }
    )

    assert text.startswith("- `20260716-212758-81c7a82b` · **第二版** · ")
    assert "restore-" not in text
    assert "可恢复文件" not in text
    assert "仅记录数据" not in text


def test_checkpoint_tool_ledger_redacts_credentials_and_raw_output():
    from aero.cli.main import _checkpoint_tool_ledger
    from aero.core.types import ToolCall

    messages = [
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="download_data",
                    arguments={"api_key": "secret", "output_dir": "data"},
                )
            ],
        ),
        Message(
            role="tool",
            content='{"status":"success","file_path":"data/result.nc","token":"leak"}',
            tool_call_id="call-1",
        ),
    ]

    ledger = _checkpoint_tool_ledger(messages)

    assert ledger[0]["arguments"]["api_key"] == "***"
    assert ledger[0]["outputs"] == ["data/result.nc"]
    assert "secret" not in str(ledger)
    assert "leak" not in str(ledger)


def test_session_title_skips_greeting_and_summarizes_request():
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="你好"),
        Message(role="assistant", content="你好！"),
        Message(role="user", content="帮我下载 2025 年 7 月 8 日的 ERA5 500hPa 位势高度"),
    ]

    assert _session_title_from_messages(messages) == "下载 2025 年 7 月 8 日的 ERA5…"


def test_only_agent_conversation_messages_are_persistable_sessions():
    assert _has_persistable_session_messages([Message(role="system", content="system")]) is False
    assert (
        _has_persistable_session_messages(
            [
                Message(role="system", content="system"),
                Message(role="user", content="[compact_summary]\nold context"),
            ]
        )
        is False
    )
    assert (
        _has_persistable_session_messages(
            [
                Message(role="system", content="system"),
                Message(role="user", content="下载 ERA5 数据"),
            ]
        )
        is True
    )


def test_session_title_falls_back_to_greeting_when_only_greeting_exists():
    messages = [Message(role="user", content="你好")]

    assert _session_title_from_messages(messages) == "你好"


def test_generated_session_title_is_normalized():
    assert (
        _normalize_generated_session_title('标题："ERA5 降水下载分析。"\n解释')
        == "ERA5 降水下载分析"
    )
    assert _normalize_generated_session_title("  # 我的新会话！ ") == "我的新会话"


def test_session_title_prompt_uses_first_exchange():
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="下载 ERA5 数据"),
        Message(role="assistant", content="好的，我来处理。"),
        Message(role="user", content="第二个需求"),
    ]

    prompt = _session_title_prompt(messages, "zh")

    assert "下载 ERA5 数据" in prompt
    assert "好的，我来处理。" in prompt
    assert "第二个需求" not in prompt
    assert "不要解释" in prompt


def test_checkpoint_title_uses_latest_meaningful_request():
    messages = [
        Message(role="user", content="下载 ERA5 数据"),
        Message(role="assistant", content="已经下载完成。"),
        Message(role="user", content="继续完成臭氧敏感性分析并保存结果"),
        Message(role="assistant", content="分析结果已经保存。"),
    ]

    assert _checkpoint_title_from_messages(messages) == "继续完成臭氧敏感性分析并保存结果"


def test_checkpoint_title_prompt_uses_recent_work_and_forbids_generic_name():
    messages = [
        Message(role="user", content="下载 CAMS 臭氧数据"),
        Message(role="assistant", content="下载完成。"),
        Message(role="user", content="绘制全球臭氧分布图"),
        Message(role="assistant", content="图片已经生成。"),
    ]

    prompt = _checkpoint_title_prompt(messages, "zh")

    assert "绘制全球臭氧分布图" in prompt
    assert "图片已经生成" in prompt
    assert "不要使用‘检查点’三个字" in prompt
    assert "6到16个中文字符" in prompt


def test_checkpoint_title_removes_generic_checkpoint_wording():
    assert _normalize_checkpoint_title("标题：全球臭氧制图检查点。") == "全球臭氧制图"
    assert _normalize_checkpoint_title("Checkpoint") == ""


@pytest.mark.asyncio
async def test_checkpoint_title_is_generated_by_model(monkeypatch):
    prompts = []

    class FakeClient:
        def __init__(self, config):
            self.config = config

        async def chat(self, messages):
            prompts.append(messages[0].content)
            return "标题：CAMS 臭氧制图完成。"

        async def close(self):
            return None

    monkeypatch.setattr("aero.agent.llm_client.LLMClient", FakeClient)
    app = AeroApp.__new__(AeroApp)
    app.agent = SimpleNamespace(messages=[Message(role="user", content="绘制 CAMS 全球臭氧图")])
    app.config = SimpleNamespace(
        language="zh",
        llm=SimpleNamespace(
            provider="test",
            model="test-model",
            base_url="https://example.invalid",
            active_api_key=lambda: "test-key",
        ),
    )

    title = await AeroApp._generate_checkpoint_title(app)

    assert title == "CAMS 臭氧制图完成"
    assert prompts and "绘制 CAMS 全球臭氧图" in prompts[0]


def test_user_theme_preference_persists(tmp_path, monkeypatch):
    pref_path = tmp_path / "preferences.yaml"
    pref_path.write_text("language: zh\nui:\n  density: compact\n")
    monkeypatch.setenv("AERO_PREFERENCES_PATH", str(pref_path))

    _save_user_theme("dracula")

    assert _load_saved_theme() == "dracula"
    text = pref_path.read_text()
    assert "density: compact" in text
    assert "theme: dracula" in text


@pytest.mark.asyncio
async def test_chat_input_uses_compact_editor_rows_without_vertical_inset(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    app = AeroApp(AeroConfig.create_default(), persist_config=False)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        user_input = app.query_one("#user-input")
        input_meta = app.query_one("#input-meta")

        assert input_box.region.height == 5
        assert user_input.region.height == 2
        assert user_input.styles.padding.top == 0
        assert user_input.styles.padding.bottom == 0
        assert user_input.content_region.height == 2
        assert input_meta.region.height == 1
        assert user_input.content_region.x == input_meta.content_region.x
