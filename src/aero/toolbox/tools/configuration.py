"""CDS and language-model configuration tools."""

import re
from pathlib import Path

from aero.core.config import (
    AeroConfig,
    clear_cds_credentials,
    clear_llm_api_key,
    save_cds_credentials,
    save_llm_profile,
    user_secrets_path,
)
from aero.core.llm_providers import (
    BUILTIN_LLM_PROVIDERS,
    get_provider_preset,
    model_alias_for_provider,
    normalize_provider_id,
)
from aero.toolbox.config_access import find_config, find_config_path, mask_secret
from aero.toolbox.registry import register_tool


@register_tool(
    name="check_cds_config",
    description=(
        "检查 CDS API 是否已配置就绪。"
        "不要提前调用，仅当 download_era5 返回 CDS API key 未配置时才使用。"
        "如果未配置，引导用户提供 API key 并使用 configure_cds_key 保存。"
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)
def check_cds_config() -> dict:
    """Check whether CDS API credentials are configured.

    Returns status and guidance for the user.
    """
    config = find_config()
    cds_cfg = config.credentials.cds

    if cds_cfg.key:
        return {
            "status": "ready",
            "message": "CDS API 已配置，可以直接下载数据。",
            "url": cds_cfg.url,
        }

    return {
        "status": "not_configured",
        "message": (
            "CDS API 未配置。请引导用户完成以下步骤：\n"
            "1. 访问 https://cds.climate.copernicus.eu/ 注册账户\n"
            "2. 进入 User Profile → API key\n"
            "3. 直接原样粘贴页面上的两行官方配置：url: ... 和 key: ...\n"
            "收到用户粘贴的凭证后，立即保存配置。"
        ),
    }


@register_tool(
    name="configure_cds_key",
    description=(
        "保存 CDS API 的 URL 和 Key 到用户级密钥文件。"
        "当用户粘贴 CDS 官方两行凭证（url: ...\\nkey: ...）时调用此工具；也兼容旧的单行凭证。"
        "成功后用户即可下载数据。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "credential_string": {
                "type": "string",
                "description": "用户粘贴的凭证字符串，通常是官方两行格式：url: ...\\nkey: ...",
            },
        },
        "required": ["credential_string"],
    },
)
def configure_cds_key(credential_string: str) -> dict:
    """Parse CDS credential string and save to the user secrets file.

    Supports formats:
    - "https://cds.climate.copernicus.eu/api:xxxx-xxxx-xxxx-xxxx"
    - "url: https://cds.climate.copernicus.eu/api\nkey: xxxx-xxxx-xxxx-xxxx"
    """
    text = credential_string.strip()

    if "\n" in text:
        parts = text.split("\n")
        url = ""
        key = ""
        for p in parts:
            p = p.strip()
            if p.lower().startswith("url:"):
                url = p.split(":", 1)[1].strip()
            elif p.lower().startswith("key:"):
                key = p.split(":", 1)[1].strip()
        if url and key:
            cds_url, cds_key = url, key
        else:
            return {
                "status": "error",
                "message": "无法解析凭证，请直接粘贴官方显示的两行配置：url: ... 和 key: ...",
            }
    else:
        m = re.match(r"^(https?://[^:]+)(?::(.+))?$", text)
        if m:
            cds_url = m.group(1)
            cds_key = m.group(2) or ""
        else:
            return {
                "status": "error",
                "message": "格式不正确，请直接粘贴官方显示的两行配置：url: ... 和 key: ...",
            }

    if not cds_key:
        return {
            "status": "error",
            "message": "未找到 key，请确认粘贴了完整的两行内容：url: ... 和 key: ...",
        }

    config = find_config()
    config.credentials.cds.url = cds_url
    config.credentials.cds.key = cds_key
    save_cds_credentials(cds_url, cds_key)

    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        config_path = parent / "aero.yaml"
        if config_path.exists():
            config.save(config_path)
            return {
                "status": "success",
                "message": (f"CDS API key 已保存到 {user_secrets_path()}，现在可以开始下载数据。"),
                "secrets_path": str(user_secrets_path()),
            }

    config.save(cwd / "aero.yaml")
    return {
        "status": "success",
        "message": "CDS API key 已保存到用户级密钥文件，现在可以开始下载数据。",
        "secrets_path": str(user_secrets_path()),
    }


@register_tool(
    name="list_llm_providers",
    description=(
        "列出 Aero 内置支持的 LLM 提供商、默认模型和 API key 获取入口。"
        "当用户询问可以用哪些模型服务商、在哪里拿 key、如何配置 LLM 时调用。"
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)
def list_llm_providers() -> dict:
    """List built-in OpenAI-compatible LLM provider presets."""
    return {
        "status": "success",
        "providers": [
            {
                "id": preset.id,
                "name": preset.name,
                "default_model": preset.default_model,
                "models": list(preset.models),
                "api_key_url": preset.api_key_url,
                "api_key_hint": preset.api_key_hint,
            }
            for preset in BUILTIN_LLM_PROVIDERS.values()
        ],
        "custom_supported": True,
        "message": (
            "可先选择内置提供商：DeepSeek、阿里云百炼、Kimi、OpenAI。"
            "Qwen/通义千问系列默认使用阿里云百炼官方接口。"
            "如果不在列表中，也可以提供 OpenAI 兼容的 base_url 自定义配置。"
        ),
    }


@register_tool(
    name="configure_llm_provider",
    description=(
        "保存或切换当前对话使用的 LLM 提供商、模型和 API key。"
        "当用户选择 DeepSeek/阿里云百炼/Kimi/OpenAI，或粘贴新的 LLM API key 时调用。"
        "如果用户只提供新的 key，可以沿用当前 provider 和 model。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "description": (
                    "提供商 id 或别名。内置：deepseek、bailian、kimi、openai；也支持 custom。"
                ),
            },
            "api_key": {
                "type": "string",
                "description": "用户提供的新 LLM API key。",
            },
            "model": {
                "type": "string",
                "description": "可选模型名。不传时使用提供商默认模型，或沿用当前模型。",
            },
            "base_url": {
                "type": "string",
                "description": (
                    "自定义 OpenAI 兼容 base_url。内置提供商通常不用传；custom provider 必须传。"
                ),
            },
        },
        "required": ["api_key"],
    },
)
def configure_llm_provider(
    api_key: str,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> dict:
    """Save or switch OpenAI-compatible LLM credentials."""
    config_path = find_config_path()
    config = (
        AeroConfig.load(config_path) if config_path.exists() else AeroConfig.create_default()
    )

    provider_was_explicit = bool(provider and provider.strip())
    raw_provider = (provider or "").strip()
    provider_model_alias = model_alias_for_provider(raw_provider)
    if provider_model_alias is not None:
        provider_id, alias_model = provider_model_alias
        if not model:
            model = alias_model
    else:
        provider_id = normalize_provider_id(provider or config.llm.provider or "deepseek")
    preset = get_provider_preset(provider_id)
    if provider_id != "custom" and preset is None:
        available = ", ".join(BUILTIN_LLM_PROVIDERS)
        return {
            "status": "error",
            "message": (
                f"暂不认识这个提供商：{provider}。"
                f"可选：{available}，或使用 custom 并提供 base_url。"
            ),
        }

    cleaned_key = api_key.strip()
    if not cleaned_key:
        return {"status": "error", "message": "API key 不能为空。"}

    cleaned_base_url = (base_url or "").strip()
    if preset is not None:
        display_name = preset.name
        existing_provider_config = config.llm.providers.get(provider_id)
        existing_base_url = existing_provider_config.base_url if existing_provider_config else ""
        existing_model = existing_provider_config.model if existing_provider_config else ""
        cleaned_base_url = cleaned_base_url or existing_base_url or preset.base_url
        if provider_was_explicit and provider_id != config.llm.provider:
            selected_model = (model or "").strip() or existing_model or preset.default_model
        else:
            selected_model = (
                (model or "").strip() or existing_model or config.llm.model or preset.default_model
            )
    else:
        display_name = "自定义提供商"
        if not cleaned_base_url:
            return {
                "status": "error",
                "message": "自定义提供商需要提供 OpenAI 兼容 base_url。",
            }
        selected_model = (model or "").strip() or config.llm.model

    config.llm.apply_active_provider_defaults()
    config.llm.switch_provider(provider_id)
    provider_config = config.llm.provider_config(provider_id)
    provider_config.api_key = cleaned_key
    provider_config.base_url = cleaned_base_url
    if selected_model:
        provider_config.model = selected_model
    config.llm.use_provider_settings()
    save_llm_profile(provider_id, cleaned_key, config.llm.model, config.llm.base_url)

    config.save(config_path)
    return {
        "status": "success",
        "message": f"{display_name} 已配置完成，当前模型为 {config.llm.model}。",
        "llm_config_updated": True,
        "provider": config.llm.provider,
        "provider_name": display_name,
        "model": config.llm.model,
        "base_url": config.llm.base_url,
        "api_key_masked": mask_secret(cleaned_key),
        "config_path": str(config_path),
        "secrets_path": str(user_secrets_path()),
    }


@register_tool(
    name="clear_llm_config",
    description=(
        "清除用户级密钥文件中已保存的 LLM API key，方便重新配置或测试首次启动流程。"
        "默认只清除 api_key，保留当前 provider/model/base_url。"
        "只有用户明确要求重置模型服务商时，才传 reset_provider=true。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "reset_provider": {
                "type": "boolean",
                "description": "是否同时重置 provider/model/base_url 到默认 DeepSeek 配置。",
            },
        },
    },
)
def clear_llm_config(reset_provider: bool = False) -> dict:
    """Clear saved LLM API key from the user secrets file."""
    config_path = find_config_path()
    config = (
        AeroConfig.load(config_path) if config_path.exists() else AeroConfig.create_default()
    )

    had_key = bool(config.llm.active_api_key())
    previous_provider = config.llm.provider
    config.llm.set_active_api_key("")
    clear_llm_api_key(previous_provider)
    if reset_provider:
        preset = get_provider_preset("deepseek")
        config.llm.switch_provider("deepseek")
        config.llm.model = preset.default_model if preset else "deepseek-chat"
        config.llm.base_url = preset.base_url if preset else ""
        provider_config = config.llm.provider_config("deepseek")
        provider_config.model = config.llm.model
        provider_config.base_url = config.llm.base_url
        provider_config.api_key = ""
        clear_llm_api_key("deepseek")

    config.save(config_path)
    return {
        "status": "success",
        "message": ("LLM API key 已清除。" if had_key else "当前没有已保存的 LLM API key。"),
        "llm_config_updated": True,
        "provider": config.llm.provider,
        "model": config.llm.model,
        "base_url": config.llm.base_url,
        "api_key_cleared": True,
        "reset_provider": reset_provider,
        "config_path": str(config_path),
        "secrets_path": str(user_secrets_path()),
    }


@register_tool(
    name="clear_cds_config",
    description=(
        "清除用户级密钥文件中已保存的 CDS API 凭证（url 和 key）。"
        "用户要求「清除密钥」「删除 CDS 配置」「清空凭证」时调用此工具。"
        "清除后如需下载数据需重新配置。"
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)
def clear_cds_config() -> dict:
    """Clear CDS API credentials from the user secrets file."""
    from aero.core.config import CDSCredentials

    config_path = None
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        p = parent / "aero.yaml"
        if p.exists():
            config_path = p
            break

    config = find_config() if config_path is not None else AeroConfig.create_default()
    was_configured = bool(config.credentials.cds.key)
    config.credentials.cds = CDSCredentials()
    clear_cds_credentials()
    if config_path is not None:
        config.save(config_path)

    if was_configured:
        return {
            "status": "success",
            "message": (
                f"CDS API 凭证已从 {user_secrets_path()} 中清除。"
                "如需重新下载，请提供新的 CDS API key。"
            ),
            "secrets_path": str(user_secrets_path()),
        }
    return {
        "status": "success",
        "message": "用户级密钥文件中未配置 CDS 凭证，无需操作。",
        "secrets_path": str(user_secrets_path()),
    }
