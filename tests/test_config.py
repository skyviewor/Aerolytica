"""Tests for Aero config module."""

import pytest
import yaml
from pydantic import ValidationError

from aero.core.config import AeroConfig
from aero.core.llm_providers import (
    BUILTIN_LLM_PROVIDERS,
    MODEL_METADATA,
    get_provider_preset,
    model_alias_for_provider,
    model_summary,
    model_supports_vision,
    normalize_provider_id,
)


def test_create_default_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "missing-secrets.yaml"))
    config = AeroConfig.create_default()
    assert config.llm.provider == "deepseek"
    assert config.llm.model == "deepseek-v4-flash"
    assert config.llm.reasoning_effort == ""
    assert config.output.data_dir == "data"


def test_load_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "missing-secrets.yaml"))
    data = {
        "llm": {
            "provider": "openai",
            "model": "gpt-4o",
            "reasoning_effort": "medium",
            "api_key": "sk-test",
        },
        "output": {"data_dir": "my_data"},
    }
    config_path = tmp_path / "aero.yaml"
    config_path.write_text(yaml.dump(data))

    config = AeroConfig.load(config_path)
    assert config.llm.provider == "openai"
    assert config.llm.model == "gpt-4o"
    assert config.llm.reasoning_effort == "medium"
    assert config.llm.active_api_key() == ""
    assert config.output.data_dir == "my_data"


def test_load_config_rejects_removed_project_field(tmp_path):
    config_path = tmp_path / "aero.yaml"
    config_path.write_text(yaml.dump({"project": {"name": "legacy"}}))

    with pytest.raises(ValidationError):
        AeroConfig.load(config_path)


def test_save_config(tmp_path):
    config = AeroConfig.create_default()
    config.llm.api_key = "sk-should-not-be-saved"
    config_path = tmp_path / "aero.yaml"
    config.save(config_path)
    assert config_path.exists()
    assert "sk-should-not-be-saved" not in config_path.read_text()
    AeroConfig.load(config_path)


def test_save_config_omits_api_keys(tmp_path):
    config = AeroConfig.create_default()
    config.llm.api_key = "sk-secret"
    config.credentials.cds.key = "cds-secret"
    config.vision.api_key = "vision-secret"
    config.web_search.api_key = "search-secret"
    config_path = tmp_path / "aero.yaml"

    config.save(config_path)

    text = config_path.read_text()
    assert "sk-secret" not in text
    assert "cds-secret" not in text
    assert "vision-secret" not in text
    assert "search-secret" not in text


def test_load_config_applies_user_secrets(tmp_path, monkeypatch):
    secrets_path = tmp_path / "secrets.yaml"
    secrets_path.write_text(
        yaml.dump(
            {
                "llm": {"providers": {"deepseek": {"api_key": "sk-global"}}},
                "credentials": {"cds": {"url": "https://cds.example/api", "key": "cds-global"}},
                "vision": {"api_key": "vision-global"},
            }
        )
    )
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))
    config_path = tmp_path / "aero.yaml"
    AeroConfig.create_default().save(config_path)

    loaded = AeroConfig.load(config_path)

    assert loaded.llm.active_api_key() == "sk-global"
    assert loaded.credentials.cds.url == "https://cds.example/api"
    assert loaded.credentials.cds.key == "cds-global"
    assert loaded.vision.api_key == "vision-global"


def test_web_search_secret_does_not_override_vision_model(tmp_path, monkeypatch):
    from aero.core.config import save_web_search_api_key

    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))
    config = AeroConfig.create_default()
    config.vision.mode = "separate"
    config.vision.provider = "bailian"
    config.vision.model = "qwen3.5-flash"
    config.vision.api_key = "sk-vision"
    config_path = tmp_path / "aero.yaml"
    config.save(config_path)

    save_web_search_api_key("sk-search", model="qwen-turbo")
    loaded = AeroConfig.load(config_path)

    assert loaded.vision.model == "qwen3.5-flash"
    assert loaded.vision.api_key == ""
    assert loaded.web_search.model == "qwen-turbo"
    assert loaded.web_search.api_key == "sk-search"


def test_legacy_web_search_secret_no_longer_overrides_vision_model(tmp_path, monkeypatch):
    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))
    secrets_path.write_text(
        yaml.dump(
            {
                "vision": {
                    "provider": "bailian",
                    "model": "qwen-turbo",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "api_key": "sk-legacy-search",
                }
            }
        )
    )
    config = AeroConfig.create_default()
    config.vision.mode = "separate"
    config.vision.model = "qwen3.5-flash"
    config_path = tmp_path / "aero.yaml"
    config.save(config_path)

    loaded = AeroConfig.load(config_path)

    assert loaded.vision.model == "qwen3.5-flash"
    assert loaded.vision.api_key == "sk-legacy-search"
    assert loaded.web_search.api_key == "sk-legacy-search"


def test_default_config_uses_global_active_llm_profile(tmp_path, monkeypatch):
    secrets_path = tmp_path / "secrets.yaml"
    secrets_path.write_text(
        yaml.dump(
            {
                "llm": {
                    "active_provider": "kimi",
                    "providers": {
                        "kimi": {
                            "api_key": "sk-kimi-global",
                            "model": "kimi-k2.6",
                            "base_url": "https://api.moonshot.cn/v1",
                        }
                    },
                }
            }
        )
    )
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))

    config = AeroConfig.create_default()

    assert config.llm.provider == "kimi"
    assert config.llm.model == "kimi-k2.6"
    assert config.llm.base_url == "https://api.moonshot.cn/v1"
    assert config.llm.active_api_key() == "sk-kimi-global"


@pytest.mark.parametrize(
    ("provider", "model", "base_url"),
    [
        ("minimax", "MiniMax-M3", "https://api.minimaxi.com/v1"),
        ("zhipu", "glm-4.6v-flashx", "https://open.bigmodel.cn/api/paas/v4"),
    ],
)
def test_removed_llm_profile_falls_back_to_default_setup(
    tmp_path, monkeypatch, provider, model, base_url
):
    secrets_path = tmp_path / "secrets.yaml"
    secrets_path.write_text(
        yaml.dump(
            {
                "llm": {
                    "active_provider": provider,
                    "providers": {
                        provider: {
                            "api_key": "sk-removed",
                            "model": model,
                            "base_url": base_url,
                        }
                    },
                }
            }
        )
    )
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))

    config = AeroConfig.create_default()

    assert config.llm.provider == "deepseek"
    assert config.llm.model == "deepseek-v4-flash"
    assert config.llm.base_url == ""
    assert config.llm.active_api_key() == ""
    if provider == "zhipu":
        assert config.web_search.provider == "zhipu"
        assert config.web_search.api_key == "sk-removed"


def test_project_provider_api_keys_are_ignored(tmp_path, monkeypatch):
    secrets_path = tmp_path / "missing-secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))
    config_path = tmp_path / "aero.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "llm": {
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "providers": {
                        "deepseek": {
                            "api_key": "sk-project-secret",
                            "model": "deepseek-chat",
                        }
                    },
                },
                "credentials": {"cds": {"key": "cds-project-secret"}},
                "vision": {"api_key": "vision-project-secret"},
            }
        )
    )

    loaded = AeroConfig.load(config_path)

    assert loaded.llm.active_api_key() == ""
    assert loaded.llm.model == "deepseek-v4-flash"
    assert loaded.llm.providers["deepseek"].model == "deepseek-v4-flash"
    assert loaded.credentials.cds.key == ""
    assert loaded.vision.api_key == ""


def test_config_missing_file():
    try:
        AeroConfig.load("/nonexistent/path/aero.yaml")
        assert False, "Expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_llm_provider_presets():
    from aero.data.vision_models import VISION_MODELS

    assert normalize_provider_id("阿里云百炼") == "bailian"
    assert normalize_provider_id("qwen3.7") == "bailian"
    assert "siliconflow" not in BUILTIN_LLM_PROVIDERS
    assert list(BUILTIN_LLM_PROVIDERS)[:2] == ["bailian", "deepseek"]
    assert model_alias_for_provider("qwen3.7") == ("bailian", "qwen3.7-plus")
    preset = get_provider_preset("kimi")
    assert preset is not None
    assert preset.default_model
    assert preset.base_url.endswith("/v1")
    for provider_id, provider_preset in BUILTIN_LLM_PROVIDERS.items():
        assert all(model_summary(provider_id, model) != "文本" for model in provider_preset.models)
    assert model_summary("bailian", "qwen3.7-plus").startswith("多模态")
    assert model_supports_vision("bailian", "qwen3.7-plus") is True
    assert model_supports_vision("bailian", "qwen3.7-flash") is True
    assert model_supports_vision("bailian", "qwen3.7-max") is False
    assert "qwen3.7-flash" in dict(VISION_MODELS)
    assert {model for model, _ in VISION_MODELS} == {
        model
        for (provider, model), metadata in MODEL_METADATA.items()
        if provider == "bailian" and metadata.supports_vision
    }


def test_active_model_capabilities_refresh_when_switching_provider_profile():
    config = AeroConfig.create_default()
    config.llm.provider = "kimi"
    profile = config.llm.provider_config("kimi")
    profile.model = "kimi-k2.6"
    profile.api_key = "sk-kimi-test"

    config.llm.use_provider_settings()

    assert config.llm.model == "kimi-k2.6"
    assert config.llm.supports_vision is True


def test_configure_llm_provider_tool(tmp_path, monkeypatch):
    from aero.toolbox.builtin_tools import clear_llm_config, configure_llm_provider

    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))
    config = AeroConfig.create_default()
    config.llm.model = "deepseek-v4-flash"
    config_path = tmp_path / "aero.yaml"
    config.save(config_path)
    monkeypatch.chdir(tmp_path)

    result = configure_llm_provider(api_key="sk-test-0002")

    assert result["status"] == "success"
    assert result["llm_config_updated"] is True
    assert result["api_key_masked"] == "sk-t...0002"
    assert "sk-test-0002" not in result["message"]
    assert "sk-test-0002" in secrets_path.read_text()
    assert "sk-test-0002" not in config_path.read_text()

    loaded = AeroConfig.load(config_path)
    assert loaded.llm.provider == "deepseek"
    assert loaded.llm.active_api_key() == "sk-test-0002"
    assert loaded.llm.providers["deepseek"].api_key == "sk-test-0002"
    assert loaded.llm.model == "deepseek-v4-flash"

    result = configure_llm_provider(provider="kimi", api_key="sk-test-0003")
    assert result["status"] == "success"
    assert "sk-test-0003" in secrets_path.read_text()
    assert "sk-test-0003" not in config_path.read_text()
    loaded = AeroConfig.load(config_path)
    assert loaded.llm.provider == "kimi"
    assert loaded.llm.model == "kimi-k3"
    assert loaded.llm.base_url == "https://api.moonshot.cn/v1"
    assert loaded.llm.providers["deepseek"].api_key == "sk-test-0002"
    assert loaded.llm.providers["kimi"].api_key == "sk-test-0003"

    result = configure_llm_provider(provider="qwen3.7", api_key="sk-test-0004")
    assert result["status"] == "success"
    loaded = AeroConfig.load(config_path)
    assert loaded.llm.provider == "bailian"
    assert loaded.llm.model == "qwen3.7-plus"
    assert loaded.llm.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert loaded.llm.providers["bailian"].api_key == "sk-test-0004"
    loaded.llm.switch_provider("deepseek")
    assert loaded.llm.active_api_key() == "sk-test-0002"

    result = clear_llm_config()
    assert result["status"] == "success"
    assert result["llm_config_updated"] is True
    assert result["api_key_cleared"] is True
    loaded = AeroConfig.load(config_path)
    assert loaded.llm.active_api_key() == ""
    assert loaded.llm.providers["bailian"].api_key == ""
    assert loaded.llm.providers["deepseek"].api_key == "sk-test-0002"
    assert loaded.llm.provider == "bailian"
    assert loaded.llm.model == "qwen3.7-plus"

    result = configure_llm_provider(provider="kimi", api_key="sk-test-0005")
    assert result["status"] == "success"
    result = clear_llm_config(reset_provider=True)
    assert result["status"] == "success"
    loaded = AeroConfig.load(config_path)
    assert loaded.llm.active_api_key() == ""
    assert loaded.llm.provider == "deepseek"
    assert loaded.llm.model == "deepseek-v4-flash"


def test_configure_cds_key_accepts_official_two_line_format(tmp_path, monkeypatch):
    from aero.toolbox.builtin_tools import configure_cds_key

    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))
    config_path = tmp_path / "aero.yaml"
    AeroConfig.create_default().save(config_path)
    monkeypatch.chdir(tmp_path)

    result = configure_cds_key(
        "url: https://cds.climate.copernicus.eu/api\nkey: ee3a913f-03e7-4c83-bbbb-ed422aa0e091"
    )

    assert result["status"] == "success"
    secrets_text = secrets_path.read_text()
    assert "https://cds.climate.copernicus.eu/api" in secrets_text
    assert "ee3a913f-03e7-4c83-bbbb-ed422aa0e091" in secrets_text
    assert "ee3a913f-03e7-4c83-bbbb-ed422aa0e091" not in config_path.read_text()


def test_configure_ads_key_accepts_token_and_keeps_it_out_of_project_config(tmp_path, monkeypatch):
    from aero.toolbox.builtin_tools import check_ads_config, configure_ads_key

    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))
    config_path = tmp_path / "aero.yaml"
    AeroConfig.create_default().save(config_path)
    monkeypatch.chdir(tmp_path)

    result = configure_ads_key("ads-token-0001")

    assert result["status"] == "success"
    assert result["url"] == "https://ads.atmosphere.copernicus.eu/api"
    assert "ads-token-0001" in secrets_path.read_text()
    assert "ads-token-0001" not in config_path.read_text()

    loaded = AeroConfig.load(config_path)
    assert loaded.credentials.ads.key == "ads-token-0001"
    assert check_ads_config()["status"] == "ready"


def test_check_ads_config_lists_direct_cams_terms_urls(tmp_path, monkeypatch):
    from aero.toolbox.builtin_tools import check_ads_config

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))

    result = check_ads_config()

    assert result["status"] == "not_configured"
    assert (
        "https://ads.atmosphere.copernicus.eu/datasets/cams-global-reanalysis-eac4?tab=download"
    ) in result["message"]
    assert (
        "https://ads.atmosphere.copernicus.eu/datasets/"
        "cams-global-atmospheric-composition-forecasts?tab=download"
    ) in result["message"]


def test_configure_earthdata_token_saves_user_secret_only(tmp_path, monkeypatch):
    from aero.toolbox.builtin_tools import check_earthdata_config, configure_earthdata_token

    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))
    config_path = tmp_path / "aero.yaml"
    AeroConfig.create_default().save(config_path)
    monkeypatch.chdir(tmp_path)

    result = configure_earthdata_token("Bearer earthdata-token-0001")

    assert result["status"] == "success"
    assert result["token_masked"] == "eart...0001"
    assert "earthdata-token-0001" in secrets_path.read_text()
    assert "earthdata-token-0001" not in config_path.read_text()

    loaded = AeroConfig.load(config_path)
    assert loaded.credentials.earthdata.token == "earthdata-token-0001"
    assert check_earthdata_config()["status"] == "ready"


def test_check_earthdata_config_uses_current_profile_labels(tmp_path, monkeypatch):
    from aero.toolbox.builtin_tools import check_earthdata_config

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))

    result = check_earthdata_config()

    assert result["status"] == "not_configured"
    assert "My Profile" in result["message"]
    assert "Access Token" in result["message"]
    assert "Generate Token" in result["message"]
    assert "Create Token" not in result["message"]


def test_check_web_search_config_explains_both_provider_flows(tmp_path, monkeypatch):
    from aero.toolbox.builtin_tools import check_web_search_config

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config = AeroConfig.create_default()
    config.web_search.provider = "bailian"
    config.save(tmp_path / "aero.yaml")
    monkeypatch.chdir(tmp_path)

    result = check_web_search_config()

    assert result["provider"] == "bailian"
    assert result["api_key_configured"] is False
    assert "阿里云百炼 WebSearch MCP" in result["message"]
    assert "智谱 AI 搜索" in result["message"]
    assert "立即开通" in result["message"]
    assert "确认开通" in result["message"]
    assert "两项缺一不可" in result["message"]
    assert "无需开通百炼 MCP" in result["message"]
    assert "/websearch provider" in result["message"]
    assert "前 2000 次调用免费" in result["message"]
    assert "29 元/千次" in result["message"]
    assert "search_std 0.01 元/次" in result["message"]
    assert "完整展示“阿里云百炼”和“智谱 AI”两条方案" in result["message"]
    assert result["providers"][0]["pricing"] == {
        "free_calls": 2000,
        "price_cny_per_1000_calls_after_free_quota": 29,
        "note": "价格可能调整，以阿里云官方计费页面为准",
        "url": "https://help.aliyun.com/zh/model-studio/web-search-mcp",
    }
    assert result["providers"][1]["pricing"]["search_std_cny_per_call"] == 0.01
    assert result["references"] == [
        "https://help.aliyun.com/zh/model-studio/web-search-mcp",
        "https://docs.bigmodel.cn/cn/guide/tools/web-search",
    ]
    assert {provider["id"] for provider in result["providers"]} == {
        "bailian",
        "zhipu",
    }


def test_web_search_config_offers_explicit_bailian_key_reuse(tmp_path, monkeypatch):
    from aero.core.config import save_llm_profile
    from aero.toolbox.builtin_tools import (
        authorize_web_search_key_reuse,
        check_web_search_config,
    )

    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))
    config_path = tmp_path / "aero.yaml"
    AeroConfig.create_default().save(config_path)
    save_llm_profile(
        "bailian",
        "sk-bailian-existing",
        "qwen3.7-plus",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.chdir(tmp_path)

    checked = check_web_search_config()

    assert checked["reuse_available"]["bailian"] is True
    assert checked["configuration_state"] == "reusable_key_available"
    assert checked["dedicated_web_search_key_configured"] is False
    assert checked["reusable_model_api_key_detected"] is True
    assert checked["message"].startswith("已检测到可复用的模型 API Key")
    assert "可以复用，无需重新创建或输入" in checked["message"]
    assert "明确授权复用现有百炼 API Key" in checked["message"]
    assert "仍需到百炼 MCP 广场开通" in checked["message"]
    assert "不能隐藏智谱方案" in checked["message"]
    assert "sk-bailian-existing" not in repr(checked)

    reused = authorize_web_search_key_reuse("bailian")

    assert reused["status"] == "success"
    assert reused["credential_reused"] is True
    assert reused["mcp_verified"] is False
    assert "无需重新输入 Key" in reused["action_required"]
    assert "立即开通" in reused["action_required"]
    assert "sk-bailian-existing" not in repr(reused)
    assert "sk-bailian-existing" not in config_path.read_text()
    assert "sk-bailian-existing" in secrets_path.read_text()


@pytest.mark.parametrize("historical_provider", ["dashscope", "aliyun", "qwen"])
def test_web_search_config_recognizes_historical_bailian_provider_aliases(
    tmp_path, monkeypatch, historical_provider
):
    from aero.toolbox.builtin_tools import check_web_search_config

    secrets_path = tmp_path / "secrets.yaml"
    secrets_path.write_text(
        yaml.dump(
            {
                "llm": {
                    "active_provider": historical_provider,
                    "providers": {
                        historical_provider: {
                            "api_key": "sk-historical-bailian",
                            "model": "qwen3.7-plus",
                            "base_url": ("https://dashscope.aliyuncs.com/compatible-mode/v1"),
                        }
                    },
                }
            }
        )
    )
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))
    AeroConfig.create_default().save(tmp_path / "aero.yaml")
    monkeypatch.chdir(tmp_path)

    result = check_web_search_config()

    assert result["reuse_available"]["bailian"] is True
    assert result["configuration_state"] == "reusable_key_available"
    assert "sk-historical-bailian" not in repr(result)
    loaded = AeroConfig.load(tmp_path / "aero.yaml")
    assert loaded.llm.provider == "bailian"
    assert loaded.llm.providers["bailian"].api_key == "sk-historical-bailian"


def test_vision_can_reuse_a_multimodal_primary_model(tmp_path, monkeypatch):
    from aero.core.config import resolved_vision_config, vision_is_configured

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config = AeroConfig.create_default()
    config.llm.provider = "openai"
    config.llm.model = "gpt-4o"
    config.llm.supports_vision = True
    config.llm.set_active_api_key("sk-primary")
    config.vision.mode = "reuse_primary"

    assert vision_is_configured(config) is True
    resolved = resolved_vision_config(config)
    assert resolved is not None
    assert resolved.provider == "openai"
    assert resolved.model == "gpt-4o"
    assert resolved.api_key == "sk-primary"


def test_reused_vision_snapshot_survives_text_model_switch(tmp_path, monkeypatch):
    from aero.core.config import resolved_vision_config, vision_is_configured

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config = AeroConfig.create_default()
    bailian = config.llm.provider_config("bailian")
    bailian.api_key = "sk-bailian"
    bailian.model = "qwen3.7-plus"
    bailian.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    config.vision.mode = "reuse_primary"
    config.vision.provider = "bailian"
    config.vision.model = "qwen3.7-plus"
    config.vision.base_url = bailian.base_url

    config.llm.provider = "deepseek"
    config.llm.model = "deepseek-v4-flash"
    config.llm.supports_vision = False
    config.llm.set_active_api_key("sk-deepseek")

    assert vision_is_configured(config) is True
    resolved = resolved_vision_config(config)
    assert resolved is not None
    assert resolved.provider == "bailian"
    assert resolved.model == "qwen3.7-plus"
    assert resolved.api_key == "sk-bailian"


def test_vision_provider_profile_overrides_stale_cross_provider_base_url(tmp_path, monkeypatch):
    from aero.core.config import resolved_vision_config

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config = AeroConfig.create_default()
    bailian = config.llm.provider_config("bailian")
    bailian.api_key = "sk-bailian"
    bailian.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    config.vision.mode = "separate"
    config.vision.provider = "bailian"
    config.vision.model = "qwen3.7-plus"
    config.vision.base_url = "https://api.moonshot.cn/v1"

    resolved = resolved_vision_config(config)

    assert resolved is not None
    assert resolved.api_key == "sk-bailian"
    assert resolved.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_reuse_profile_drops_a_stale_dedicated_vision_key(tmp_path, monkeypatch):
    from aero.core.config import load_user_secrets, save_vision_api_key, save_vision_profile

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    save_vision_api_key("sk-dedicated", provider="bailian", model="qwen3.7-plus")
    save_vision_profile(
        "reuse_primary",
        provider="bailian",
        model="qwen3.7-plus",
    )

    assert "api_key" not in load_user_secrets()["vision"]


def test_global_vision_profile_survives_a_fresh_project_config(tmp_path, monkeypatch):
    from aero.core.config import (
        resolved_vision_config,
        save_vision_profile,
        vision_is_configured,
    )

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    save_vision_profile(
        "separate",
        provider="bailian",
        model="qwen3.7-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    config = AeroConfig.create_default()
    bailian = config.llm.provider_config("bailian")
    bailian.api_key = "sk-bailian"
    bailian.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    assert config.vision.mode == "separate"
    assert config.vision.provider == "bailian"
    assert config.vision.model == "qwen3.7-plus"
    assert vision_is_configured(config) is True
    resolved = resolved_vision_config(config)
    assert resolved is not None
    assert resolved.model == "qwen3.7-plus"
    assert resolved.api_key == "sk-bailian"


def test_separate_vision_credentials_are_not_saved_to_project_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config = AeroConfig.create_default()
    config.vision.mode = "separate"
    config.vision.provider = "bailian"
    config.vision.model = "qwen-vl-max"
    config.vision.api_key = "sk-vision-secret"
    config_path = tmp_path / "aero.yaml"

    config.save(config_path)

    assert "sk-vision-secret" not in config_path.read_text()
    assert "mode: separate" in config_path.read_text()


def test_separate_vision_reuses_saved_bailian_profile_after_primary_switch(tmp_path, monkeypatch):
    from aero.core.config import resolved_vision_config, vision_is_configured

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config = AeroConfig.create_default()
    config.llm.provider = "deepseek"
    config.llm.model = "deepseek-v4-pro"
    config.llm.provider_config("deepseek").api_key = "sk-deepseek"
    bailian = config.llm.provider_config("bailian")
    bailian.api_key = "sk-bailian"
    bailian.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    config.vision.mode = "separate"
    config.vision.provider = "bailian"
    config.vision.model = "qwen3.6-plus"

    assert vision_is_configured(config) is True
    resolved = resolved_vision_config(config)
    assert resolved is not None
    assert resolved.provider == "bailian"
    assert resolved.model == "qwen3.6-plus"
    assert resolved.api_key == "sk-bailian"
    assert resolved.base_url == bailian.base_url


def test_save_user_secrets_is_owner_readable_only(tmp_path, monkeypatch):
    from aero.core.config import save_user_secrets

    path = tmp_path / "secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(path))
    save_user_secrets({"llm": {"providers": {"deepseek": {"api_key": "sk-test"}}}})

    assert path.stat().st_mode & 0o777 == 0o600


def test_legacy_deepseek_secret_cannot_override_selected_flash(tmp_path, monkeypatch):
    secrets_path = tmp_path / "secrets.yaml"
    secrets_path.write_text(
        yaml.dump(
            {
                "llm": {
                    "active_provider": "deepseek",
                    "providers": {
                        "deepseek": {
                            "api_key": "sk-legacy",
                            "model": "deepseek-chat",
                            "base_url": "https://api.deepseek.com",
                        }
                    },
                }
            }
        )
    )
    config_path = tmp_path / "aero.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "llm": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "providers": {
                        "deepseek": {
                            "model": "deepseek-v4-flash",
                            "base_url": "https://api.deepseek.com",
                        }
                    },
                }
            }
        )
    )
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))

    loaded = AeroConfig.load(config_path)

    assert loaded.llm.model == "deepseek-v4-flash"
    assert loaded.llm.providers["deepseek"].model == "deepseek-v4-flash"
    assert loaded.llm.active_api_key() == "sk-legacy"
