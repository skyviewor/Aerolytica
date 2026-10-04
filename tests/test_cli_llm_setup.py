"""Tests for local LLM setup helpers."""

import pytest

from aero.cli.main import (
    AeroApp,
    SecretInputScreen,
    _extract_cds_credentials,
    _extract_llm_api_key,
    _is_bare_api_key,
    _mask_cds_credentials,
    _mask_secret_text,
    _parse_llm_clear_from_text,
    _parse_llm_setup_from_text,
    _requests_primary_vision_reuse,
    _usage_meta_text,
)
from aero.core.config import AeroConfig, resolved_vision_config
from aero.core.llm_providers import BUILTIN_LLM_PROVIDERS
from aero.data.pricing import TokenTracker, context_window_for
from aero.toolbox.secret_input import SecretInputRequest

pytestmark = pytest.mark.usefixtures("byok_ui")


def test_parse_qwen_setup_uses_bailian():
    config = AeroConfig.create_default()

    setup = _parse_llm_setup_from_text(
        "配置一下 qwen3.7 模型，API key: sk-test-0004",
        config,
    )

    assert setup is not None
    assert setup["provider"] == "bailian"
    assert setup["model"] == "qwen3.7-plus"
    assert setup["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert setup["api_key"] == "sk-test-0004"


@pytest.mark.asyncio
async def test_secret_input_keyboard_actions_do_not_require_mouse():
    app = AeroApp(AeroConfig.create_default(), persist_config=False)
    result: list[str | None] = []
    request = SecretInputRequest("cds", "输入 CDS API 凭证", "url: ...\nkey: ...", True)

    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(SecretInputScreen(request), callback=result.append)
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "secret-input-value"

        await pilot.press("tab")
        await pilot.pause()
        assert app.screen._action_focused is True

        await pilot.press("right", "enter")
        await pilot.pause()

    assert result == [None]


@pytest.mark.asyncio
async def test_multiline_secret_input_enter_submits_without_adding_newline():
    app = AeroApp(AeroConfig.create_default(), persist_config=False)
    result: list[str | None] = []
    request = SecretInputRequest("cds", "输入 CDS API 凭证", "instructions", True)
    credentials = "url: https://cds.climate.copernicus.eu/api\nkey: ee3a1234-5678-9012"

    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(SecretInputScreen(request), callback=result.append)
        await pilot.pause()
        area = app.screen.query_one("#secret-input-value")
        area.load_text(credentials)

        await pilot.press("enter")
        await pilot.pause()

    assert result == [credentials]


def test_extract_official_cds_two_line_credentials_before_generic_api_key():
    text = "url: https://cds.climate.copernicus.eu/api\nkey: ee3a1234-5678-9012"

    assert _extract_cds_credentials(text) == (
        "https://cds.climate.copernicus.eu/api",
        "ee3a1234-5678-9012",
    )
    assert "ee3a1234-5678-9012" not in _mask_cds_credentials(text)
    assert "ee3a...9012" in _mask_cds_credentials(text)


def test_parse_kimi_provider_does_not_use_provider_name_as_model():
    config = AeroConfig.create_default()

    setup = _parse_llm_setup_from_text(
        "帮我配置一下 kimi 模型，api key 是: sk-test-0005",
        config,
    )

    assert setup is not None
    assert setup["provider"] == "kimi"
    assert setup["model"] == "kimi-k3"
    assert setup["base_url"] == "https://api.moonshot.cn/v1"
    assert setup["api_key"] == "sk-test-0005"


def test_parse_explicit_kimi_model():
    config = AeroConfig.create_default()

    setup = _parse_llm_setup_from_text(
        "配置 kimi-k2.7-code，API key: sk-test-0006",
        config,
    )

    assert setup is not None
    assert setup["provider"] == "kimi"
    assert setup["model"] == "kimi-k2.7-code"


def test_parse_key_only_keeps_current_provider_model():
    config = AeroConfig.create_default()
    config.llm.provider = "deepseek"
    config.llm.model = "deepseek-v4-flash"

    setup = _parse_llm_setup_from_text("换 key: sk-test-0002", config)

    assert setup is not None
    assert setup["provider"] == "deepseek"
    assert setup["model"] == "deepseek-v4-flash"
    assert setup["api_key"] == "sk-test-0002"


def test_parse_clear_key_intent_takes_priority_over_setup():
    config = AeroConfig.create_default()

    clear = _parse_llm_clear_from_text("帮我清理掉模型的 API key")
    setup = _parse_llm_setup_from_text("帮我清理掉模型的 API key", config)

    assert clear == {"reset_provider": False}
    assert setup is None


def test_parse_vision_model_setup_does_not_route_to_main_llm():
    config = AeroConfig.create_default()

    setup = _parse_llm_setup_from_text("视觉模型配置了吗？", config)
    clear = _parse_llm_clear_from_text("清除视觉模型 API key")

    assert setup is None
    assert clear is None


def test_parse_merra2_credentials_does_not_route_to_main_llm():
    config = AeroConfig.create_default()

    setup = _parse_llm_setup_from_text("怎么配置 MERRA-2 凭证", config)
    clear = _parse_llm_clear_from_text("清除 MERRA-2 Earthdata token")

    assert setup is None
    assert clear is None


def test_parse_cams_ads_credentials_does_not_route_to_main_llm():
    config = AeroConfig.create_default()

    setup = _parse_llm_setup_from_text("帮我配置 CAMS ADS API key", config)
    clear = _parse_llm_clear_from_text("清除 CAMS ADS key")

    assert setup is None
    assert clear is None


def test_parse_full_reset_llm_intent():
    clear = _parse_llm_clear_from_text("完整重置模型 API key 和服务商")

    assert clear == {"reset_provider": True}


def test_mask_secret_text():
    text = "配置 qwen3.7，API key: sk-test-0004"

    assert _extract_llm_api_key(text) == "sk-test-0004"
    assert _mask_secret_text(text) == "配置 qwen3.7，API key: sk-t...0004"


def test_bare_api_key_detection():
    assert _is_bare_api_key("sk-test-0004") is True
    assert _is_bare_api_key("API key: sk-test-0004") is False
    assert _is_bare_api_key("请使用 sk-test-0004 配置视觉模型") is False


def test_detect_primary_vision_reuse_request():
    assert _requests_primary_vision_reuse("你直接复用视觉模型的 API Key") is True
    assert _requests_primary_vision_reuse("复用主模型") is True
    assert _requests_primary_vision_reuse("切换主模型 API Key") is False


@pytest.mark.asyncio
async def test_reuse_primary_vision_uses_active_kimi_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config = AeroConfig.create_default()
    config.llm.provider = "kimi"
    kimi = config.llm.provider_config("kimi")
    kimi.model = "kimi-k2.6"
    kimi.base_url = "https://api.moonshot.cn/v1"
    kimi.api_key = "sk-kimi-test"
    config.llm.use_provider_settings()
    app = AeroApp(config, persist_config=False)

    async with app.run_test(size=(100, 30)):
        await app._process("复用主模型")

    vision = resolved_vision_config(config)
    assert vision is not None
    assert vision.provider == "kimi"
    assert vision.model == "kimi-k2.6"
    transcript = "\n".join(app._chat_log)
    assert "Kimi/kimi-k2.6" in transcript
    assert "bailian/qwen3.7-plus" not in transcript


@pytest.mark.asyncio
async def test_bare_api_key_is_masked_and_never_starts_agent():
    config = AeroConfig.create_default()
    config.vision.api_key = "sk-existing-vision"
    app = AeroApp(config, persist_config=False)

    async with app.run_test(size=(100, 30)):
        await app._process("sk-new-sensitive-value")

    transcript = "\n".join(app._chat_log)
    assert "sk-new-sensitive-value" not in transcript
    assert "sk-n...alue" in transcript
    assert app._agent_worker is None


def test_detect_vision_setup_result():
    from aero.cli.main import _credential_setup_scope, _vision_setup_required

    assert _vision_setup_required('{"status": "not_configured", "setup_required": "vision"}')
    assert not _vision_setup_required('{"status": "success"}')
    assert _credential_setup_scope('{"setup_required": "cds"}') == "cds"
    assert _credential_setup_scope('{"setup_required": "vision"}') is None


def test_all_builtin_models_have_explicit_context_windows():
    provider_models = [
        (provider, model)
        for provider, preset in BUILTIN_LLM_PROVIDERS.items()
        for model in preset.models
    ]
    models = {model for _, model in provider_models}

    # Model IDs are not globally unique: the same model may be provided by
    # both its first-party endpoint and Model Studio.
    assert len(provider_models) == 16
    assert len(models) == 14
    assert all(context_window_for(model) is not None for model in models)
    assert context_window_for("qwen3.6-flash") == 1_000_000


def test_unknown_model_has_no_assumed_context_window():
    assert context_window_for("custom-future-model") is None


def test_usage_meta_uses_qwen_one_million_context_window():
    tracker = TokenTracker(current_prompt_tokens=36_600)

    text = _usage_meta_text(tracker, "qwen3.6-flash")

    assert "上下文[/dim] 36.6K" in text
    assert "/ 4%" in text
    assert "112%" not in text


def test_usage_meta_omits_percentage_for_unknown_model():
    tracker = TokenTracker(current_prompt_tokens=36_600)

    text = _usage_meta_text(tracker, "custom-future-model")

    assert "上下文[/dim] 36.6K" in text
    assert "%" not in text


def test_input_meta_migrates_legacy_deepseek_chat_to_flash(tmp_path, monkeypatch):
    secrets_path = tmp_path / "secrets.yaml"
    secrets_path.write_text(
        "llm:\n"
        "  active_provider: deepseek\n"
        "  providers:\n"
        "    deepseek:\n"
        "      api_key: sk-legacy\n"
        "      model: deepseek-chat\n"
        "      base_url: https://api.deepseek.com\n"
    )
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))

    app = AeroApp(AeroConfig.create_default(), persist_config=False)
    text = app._input_meta_text()

    assert app.config.llm.model == "deepseek-v4-flash"
    assert "DeepSeek V4 Flash" in text
    assert "DeepSeek Chat" not in text


def test_model_status_omits_duplicate_reused_vision_model(tmp_path, monkeypatch):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config = AeroConfig.create_default()
    config.llm.provider = "bailian"
    config.llm.model = "qwen3.7-flash"
    config.llm.supports_vision = True
    config.llm.set_active_api_key("sk-test")
    config.vision.mode = "reuse_primary"
    config.vision.provider = "bailian"
    config.vision.model = "qwen3.7-flash"
    config.vision.base_url = config.llm.base_url
    app = AeroApp(config, persist_config=False)

    assert app._input_meta_text().count("qwen3.7-flash") == 1
    assert "视觉：" not in app._model_status_text(markup=False)


@pytest.mark.asyncio
async def test_model_switch_syncs_user_profile_agent_and_status(tmp_path, monkeypatch):
    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))
    monkeypatch.chdir(tmp_path)
    config = AeroConfig.create_default()
    config.llm.model = "deepseek-v4-pro"
    config.llm.set_active_api_key("sk-test-model-switch")
    config.llm.apply_active_provider_defaults()
    app = AeroApp(config, persist_config=True)

    async with app.run_test(size=(100, 30)):
        app._set_model("flash")

    reloaded = AeroConfig.create_default()
    assert app.config.llm.model == "deepseek-v4-flash"
    assert app.agent is not None
    assert app.agent.config.llm.model == "deepseek-v4-flash"
    assert app.agent.llm.config.model == "deepseek-v4-flash"
    assert "DeepSeek V4 Flash" in app._input_meta_text()
    assert reloaded.llm.model == "deepseek-v4-flash"
