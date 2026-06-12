from aero.cli.main import (
    AeroApp,
    _command_suggestions,
    _compacted_context_messages,
    _estimate_context_tokens,
    _load_saved_theme,
    _render_status_lines,
    _render_terminal_math,
    _resolve_theme_name,
    _save_user_theme,
    _help_text,
    _assistant_claims_background_handoff,
    _is_subagent_tool_status,
    _normalize_generated_session_title,
    _normalize_confirm_choice,
    _status_progress_slot,
    _should_queue_input_during_run,
    _session_title_from_messages,
    _session_title_prompt,
    _session_option_label,
    _theme_options,
    _usage_meta_text,
)
from aero.data import pricing
from aero.data.pricing import ModelPrice, TokenTracker
from aero.core.config import AeroConfig
from aero.core.types import Message, ToolCall
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
        prompt_tokens=1000,
        completion_tokens=100,
        cached_tokens=950,
        current_prompt_tokens=43_300,
    )

    text = _usage_meta_text(tracker, "deepseek-v4-flash", "qwen3.7-plus")

    assert "[dim]上下文[/dim] 43.3K [dim]/ 4%[/dim]" in text
    assert "[dim]命中缓存[/dim] 95%" in text
    assert "¥" in text
    assert "[dim]cost[/dim]" not in text
    assert "♻" not in text
    assert "(4%)" not in text


def test_session_option_label_uses_iso_time_without_usage():
    text = _session_option_label("本地数据文件清单与内容概览", 1780730520)

    assert text.startswith("本地数据文件清单与内容概览 (2026-")
    assert "T" in text
    assert "93.2K" not in text
    assert "¥" not in text


def test_command_suggestions_include_set_subcommands_after_space():
    primary = [("/set", "设置参数"), ("/session", "历史会话")]
    secondary = [
        ("/set max_tool_rounds ", "设置最大工具调用轮次"),
        ("/session rename ", "修改当前会话标题"),
    ]

    assert _command_suggestions("/", primary, secondary) == primary
    assert _command_suggestions("/set ", primary, secondary) == [
        ("/set max_tool_rounds ", "设置最大工具调用轮次")
    ]
    assert _command_suggestions("/set max", primary, secondary) == [
        ("/set max_tool_rounds ", "设置最大工具调用轮次")
    ]


def test_normalize_confirm_choice_preserves_allow_and_always():
    assert _normalize_confirm_choice("allow") == "allow"
    assert _normalize_confirm_choice("approve") == "approve"
    assert _normalize_confirm_choice("always") == "always"
    assert _normalize_confirm_choice("deny") == "deny"
    assert _normalize_confirm_choice("defer") == "deny"
    assert _normalize_confirm_choice("unexpected") == "deny"


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
        }
    )

    assert tracker.vision_cached_tokens == 800
    assert tracker.vision_cost("test-vision-cache") == ((200 * 10) + (800 * 1) + (100 * 20)) / 1000


def test_token_tracker_restores_legacy_vision_cache_field():
    tracker = TokenTracker.from_dict(
        {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "vision_prompt_tokens": 3,
            "vision_completion_tokens": 4,
        }
    )

    assert tracker.vision_cached_tokens == 0
    assert tracker.to_dict()["vision_cached_tokens"] == 0


def test_exit_session_save_is_idempotent():
    app = AeroApp.__new__(AeroApp)
    app._session_saved_on_exit = False
    calls = []
    app._auto_save_session = lambda: calls.append("saved")

    AeroApp._save_session_on_exit(app)
    AeroApp._save_session_on_exit(app)

    assert calls == ["saved"]


def test_help_text_includes_current_commands():
    text = _help_text("zh")

    assert "/vision" in text
    assert "/mode" in text
    assert "/new [标题]" in text
    assert "/session" in text
    assert "/session rename" in text
    assert "Backspace/Delete 删除" in text
    assert "/compact" in text
    assert "缓存命中" in text


def test_session_title_skips_greeting_and_summarizes_request():
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="你好"),
        Message(role="assistant", content="你好！"),
        Message(role="user", content="帮我下载 2025 年 7 月 8 日的 ERA5 500hPa 位势高度"),
    ]

    assert _session_title_from_messages(messages) == "下载 2025 年 7 月 8 日的 ERA5…"


def test_session_title_falls_back_to_greeting_when_only_greeting_exists():
    messages = [Message(role="user", content="你好")]

    assert _session_title_from_messages(messages) == "你好"


def test_generated_session_title_is_normalized():
    assert _normalize_generated_session_title("标题：\"ERA5 降水下载分析。\"\n解释") == "ERA5 降水下载分析"
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


def test_user_theme_preference_persists(tmp_path, monkeypatch):
    pref_path = tmp_path / "preferences.yaml"
    pref_path.write_text("language: zh\nui:\n  density: compact\n")
    monkeypatch.setenv("AERO_PREFERENCES_PATH", str(pref_path))

    _save_user_theme("dracula")

    assert _load_saved_theme() == "dracula"
    text = pref_path.read_text()
    assert "density: compact" in text
    assert "theme: dracula" in text
