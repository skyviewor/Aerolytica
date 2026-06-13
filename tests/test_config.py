"""Tests for Aero config module."""

import pytest
import yaml
from pydantic import ValidationError

from aero.core.config import AeroConfig
from aero.core.llm_providers import (
    BUILTIN_LLM_PROVIDERS,
    get_provider_preset,
    model_alias_for_provider,
    normalize_provider_id,
)


def test_create_default_config():
    config = AeroConfig.create_default()
    assert config.llm.provider == "deepseek"
    assert config.llm.model == "deepseek-chat"
    assert config.llm.reasoning_effort == ""
    assert config.output.data_dir == "data"


def test_load_config(tmp_path):
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
    config_path = tmp_path / "aero.yaml"

    config.save(config_path)

    text = config_path.read_text()
    assert "sk-secret" not in text
    assert "cds-secret" not in text
    assert "vision-secret" not in text


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
    assert loaded.credentials.cds.key == ""
    assert loaded.vision.api_key == ""


def test_config_missing_file():
    try:
        AeroConfig.load("/nonexistent/path/aero.yaml")
        assert False, "Expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_llm_provider_presets():
    assert normalize_provider_id("阿里云百炼") == "bailian"
    assert normalize_provider_id("qwen3.7") == "bailian"
    assert "siliconflow" not in BUILTIN_LLM_PROVIDERS
    assert model_alias_for_provider("qwen3.7") == ("bailian", "qwen3.7")
    preset = get_provider_preset("kimi")
    assert preset is not None
    assert preset.default_model
    assert preset.base_url.endswith("/v1")


def test_configure_llm_provider_tool(tmp_path, monkeypatch):
    from aero.toolbox.builtin_tools import clear_llm_config, configure_llm_provider

    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(secrets_path))
    config = AeroConfig.create_default()
    config.llm.model = "deepseek-chat"
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
    assert loaded.llm.model == "deepseek-chat"

    result = configure_llm_provider(provider="kimi", api_key="sk-test-0003")
    assert result["status"] == "success"
    assert "sk-test-0003" in secrets_path.read_text()
    assert "sk-test-0003" not in config_path.read_text()
    loaded = AeroConfig.load(config_path)
    assert loaded.llm.provider == "kimi"
    assert loaded.llm.model == "kimi-k2.6"
    assert loaded.llm.base_url == "https://api.moonshot.cn/v1"
    assert loaded.llm.providers["deepseek"].api_key == "sk-test-0002"
    assert loaded.llm.providers["kimi"].api_key == "sk-test-0003"

    result = configure_llm_provider(provider="qwen3.7", api_key="sk-test-0004")
    assert result["status"] == "success"
    loaded = AeroConfig.load(config_path)
    assert loaded.llm.provider == "bailian"
    assert loaded.llm.model == "qwen3.7"
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
    assert loaded.llm.model == "qwen3.7"

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
        "url: https://cds.climate.copernicus.eu/api\n"
        "key: ee3a913f-03e7-4c83-bbbb-ed422aa0e091"
    )

    assert result["status"] == "success"
    secrets_text = secrets_path.read_text()
    assert "https://cds.climate.copernicus.eu/api" in secrets_text
    assert "ee3a913f-03e7-4c83-bbbb-ed422aa0e091" in secrets_text
    assert "ee3a913f-03e7-4c83-bbbb-ed422aa0e091" not in config_path.read_text()


def test_configure_ads_key_accepts_token_and_keeps_it_out_of_project_config(
    tmp_path, monkeypatch
):
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
        "https://ads.atmosphere.copernicus.eu/datasets/"
        "cams-global-reanalysis-eac4?tab=download"
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
