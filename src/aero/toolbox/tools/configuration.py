"""CDS and language-model configuration tools."""

import re

from aero.core.config import (
    ADSCredentials,
    AeroConfig,
    clear_ads_credentials,
    clear_cds_credentials,
    clear_earthdata_token,
    clear_llm_api_key,
    save_ads_credentials,
    save_cds_credentials,
    save_earthdata_token,
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

    config.save(find_config_path())
    return {
        "status": "success",
        "message": "CDS API key 已保存到用户级密钥文件，现在可以开始下载数据。",
        "secrets_path": str(user_secrets_path()),
    }


@register_tool(
    name="check_ads_config",
    description=(
        "检查 Copernicus Atmosphere Data Store (ADS) API 凭证是否已配置。"
        "CAMS Reanalysis/Forecast 下载返回 ADS key 未配置，或用户问 CAMS/ADS 凭证时调用。"
    ),
    parameters={"type": "object", "properties": {}},
)
def check_ads_config() -> dict:
    """Check whether ADS API credentials are configured."""
    config = find_config()
    ads_cfg = config.credentials.ads
    if ads_cfg.key:
        return {
            "status": "ready",
            "message": "ADS API 已配置，可以下载 CAMS 数据。",
            "url": ads_cfg.url,
        }
    return {
        "status": "not_configured",
        "message": (
            "CAMS 数据来自 Copernicus Atmosphere Data Store (ADS)，需要单独配置 ADS "
            "Personal Access Token，和 ERA5/CDS key 分开。\n"
            "1. 访问 https://ads.atmosphere.copernicus.eu/ 并登录 Copernicus 账户\n"
            "2. 进入账户页面的 API token / Personal Access Token 区域\n"
            "3. 复制 ADS API 页面显示的 url/key 配置，或把 token 直接粘贴给我\n"
            "4. 首次下载 CAMS 数据集前，需要先打开对应数据集下载页接受 Terms of Use：\n"
            "   - CAMS EAC4 再分析：https://ads.atmosphere.copernicus.eu/datasets/"
            "cams-global-reanalysis-eac4?tab=download\n"
            "   - CAMS 全球大气成分预报：https://ads.atmosphere.copernicus.eu/datasets/"
            "cams-global-atmospheric-composition-forecasts?tab=download"
        ),
    }


@register_tool(
    name="configure_ads_key",
    description=(
        "保存 Copernicus ADS API URL 和 key/token 到用户级密钥文件。"
        "当用户粘贴 ADS 官方 url/key、Personal Access Token，或明确配置 CAMS/ADS 凭证时调用。"
        "不要用于 ERA5/CDS、NASA Earthdata 或 LLM API key。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "credential_string": {
                "type": "string",
                "description": (
                    "用户粘贴的 ADS 凭证；可为官方两行 url/key，也可为单独 token。"
                ),
            },
        },
        "required": ["credential_string"],
    },
)
def configure_ads_key(credential_string: str) -> dict:
    """Parse and save ADS API credentials."""
    ads_url, ads_key = _parse_ads_credentials(credential_string)
    if not ads_key:
        return {
            "status": "error",
            "message": "未找到 ADS key/token，请粘贴官方 url/key 配置或 Personal Access Token。",
        }

    config = find_config()
    config.credentials.ads.url = ads_url
    config.credentials.ads.key = ads_key
    save_ads_credentials(ads_url, ads_key)

    config_path = find_config_path()
    if config_path:
        config.save(config_path)

    return {
        "status": "success",
        "message": (
            f"ADS API key 已保存到 {user_secrets_path()}，现在可以下载 CAMS 数据。"
            "如果某个数据集首次下载失败，请打开对应数据集下载页接受 Terms of Use："
            "EAC4 再分析 https://ads.atmosphere.copernicus.eu/datasets/"
            "cams-global-reanalysis-eac4?tab=download；"
            "全球大气成分预报 https://ads.atmosphere.copernicus.eu/datasets/"
            "cams-global-atmospheric-composition-forecasts?tab=download。"
        ),
        "url": ads_url,
        "api_key_masked": mask_secret(ads_key),
        "secrets_path": str(user_secrets_path()),
    }


def _parse_ads_credentials(credential_string: str) -> tuple[str, str]:
    text = credential_string.strip()
    default_url = "https://ads.atmosphere.copernicus.eu/api"
    if not text:
        return default_url, ""
    url = default_url
    key = ""
    if "\n" in text:
        for line in text.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            name, value = line.split(":", 1)
            name = name.strip().lower()
            value = value.strip()
            if name == "url" and value:
                url = value
            elif name in {"key", "token", "api_key", "personal_access_token"}:
                key = value
        return url, key
    match = re.match(r"^(https?://[^:]+)(?::(.+))?$", text)
    if match and "atmosphere.copernicus.eu" in match.group(1):
        return match.group(1), (match.group(2) or "").strip()
    if text.lower().startswith("bearer "):
        text = text.split(None, 1)[1].strip()
    return url, text


@register_tool(
    name="check_earthdata_config",
    description=(
        "检查 NASA Earthdata/GES DISC 凭证是否已配置就绪。"
        "当用户询问如何配置 MERRA-2、GES DISC、NASA Earthdata 凭证，"
        "或 MERRA-2 下载返回授权错误时调用。不要把 Earthdata 凭证当作 LLM API key。"
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)
def check_earthdata_config() -> dict:
    """Check whether NASA Earthdata credentials are configured."""
    config = find_config()
    token = config.credentials.earthdata.token
    if token:
        return {
            "status": "ready",
            "message": "NASA Earthdata token 已配置，可以用于 MERRA-2/GES DISC 下载。",
            "token_masked": mask_secret(token),
        }

    return {
        "status": "not_configured",
        "message": (
            "MERRA-2 需要 NASA Earthdata Login/GES DISC 授权。请按以下步骤配置：\n"
            "1. 访问 https://urs.earthdata.nasa.gov/ 注册或登录 Earthdata 账户\n"
            "2. 登录后点击页面右上角 My Profile\n"
            "3. 在 My Profile 页面找到 Access Token 区域\n"
            "4. 点击 Generate Token 生成 token；如果已经有 token，可以直接复制现有 token\n"
            "5. 将 token 字符串直接粘贴给我，我会保存到本地用户级密钥文件\n\n"
            "也可以自行设置环境变量 EARTHDATA_TOKEN。"
        ),
    }


@register_tool(
    name="configure_earthdata_token",
    description=(
        "保存 NASA Earthdata token 到用户级密钥文件，用于 MERRA-2/GES DISC 下载。"
        "当用户粘贴 Earthdata token，或明确要求配置 MERRA-2/Earthdata 凭证时调用。"
        "不要用于 LLM、DeepSeek、Kimi、OpenAI、百炼或 CDS 凭证。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "token": {
                "type": "string",
                "description": "用户粘贴的 NASA Earthdata token。",
            },
        },
        "required": ["token"],
    },
)
def configure_earthdata_token(token: str) -> dict:
    """Save a NASA Earthdata token in the user secrets file."""
    value = _normalize_secret_token(token)
    if not value:
        return {"status": "error", "message": "Earthdata token 不能为空。"}

    config = find_config()
    config.credentials.earthdata.token = value
    save_earthdata_token(value)

    config_path = find_config_path()
    if config_path:
        config.save(config_path)

    return {
        "status": "success",
        "message": (
            f"NASA Earthdata token 已保存到 {user_secrets_path()}，"
            "现在可以下载 MERRA-2/GES DISC 数据。"
        ),
        "token_masked": mask_secret(value),
        "secrets_path": str(user_secrets_path()),
    }


def _normalize_secret_token(text: str) -> str:
    value = text.strip()
    if "\n" in value:
        for line in value.splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" in line and line.split(":", 1)[0].strip().lower() in {
                "token",
                "earthdata_token",
                "bearer",
            }:
                return line.split(":", 1)[1].strip()
        return ""
    if value.lower().startswith("bearer "):
        value = value.split(None, 1)[1].strip()
    return value


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
        save_llm_profile("deepseek", "", config.llm.model, config.llm.base_url)

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

    config_path = find_config_path()
    config = find_config()
    was_configured = bool(config.credentials.cds.key)
    config.credentials.cds = CDSCredentials()
    clear_cds_credentials()
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


@register_tool(
    name="clear_ads_config",
    description=(
        "清除用户级密钥文件中已保存的 ADS API 凭证。"
        "用户要求清除/删除 CAMS 或 ADS 凭证时调用。"
    ),
    parameters={"type": "object", "properties": {}},
)
def clear_ads_config() -> dict:
    """Clear ADS API credentials from the user secrets file."""
    config_path = find_config_path()
    config = find_config()
    was_configured = bool(config.credentials.ads.key)
    config.credentials.ads = ADSCredentials()
    clear_ads_credentials()
    if config_path is not None:
        config.save(config_path)
    return {
        "status": "success",
        "message": (
            f"ADS API 凭证已从 {user_secrets_path()} 中清除。"
            if was_configured
            else "用户级密钥文件中未配置 ADS 凭证，无需操作。"
        ),
        "secrets_path": str(user_secrets_path()),
    }


@register_tool(
    name="clear_earthdata_config",
    description=(
        "清除用户级密钥文件中已保存的 NASA Earthdata token。"
        "用户要求清除/删除 MERRA-2、GES DISC 或 Earthdata 凭证时调用。"
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)
def clear_earthdata_config() -> dict:
    """Clear NASA Earthdata token from the user secrets file."""
    config_path = find_config_path()
    config = find_config()
    was_configured = bool(config.credentials.earthdata.token)
    config.credentials.earthdata.token = ""
    clear_earthdata_token()
    if config_path is not None:
        config.save(config_path)

    return {
        "status": "success",
        "message": (
            f"NASA Earthdata token 已从 {user_secrets_path()} 中清除。"
            if was_configured
            else "用户级密钥文件中未配置 NASA Earthdata token，无需操作。"
        ),
        "token_cleared": was_configured,
        "secrets_path": str(user_secrets_path()),
    }
