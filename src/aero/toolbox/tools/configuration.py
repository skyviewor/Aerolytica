"""CDS and language-model configuration tools."""

import re

from aero.core.config import (
    ADSCredentials,
    AeroConfig,
    clear_ads_credentials,
    clear_cds_credentials,
    clear_earthdata_token,
    clear_llm_api_key,
    resolved_vision_config,
    save_ads_credentials,
    save_cds_credentials,
    save_earthdata_token,
    save_llm_profile,
    save_web_search_api_key,
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
from aero.toolbox.secret_input import (
    CredentialSpec,
    SecretInputRequest,
    register_credential_spec,
    request_secret_input_from_context,
    save_secret_from_context,
)


@register_tool(
    name="request_secret_input",
    description=(
        "请求安全配置凭据。云端由结构化事件显示网页配置入口，不承诺弹出本地窗口。"
        "密钥原文不会发送给模型；成功后只返回一次性 "
        "secret_handle。拿到 handle 后，调用对应配置工具并传 credential_handle 保存。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "scope": {"type": "string", "description": "凭据用途，例如 cds、ads 或 web_search。"},
            "title": {"type": "string", "description": "输入窗口标题。"},
            "instructions": {"type": "string", "description": "展示给用户的格式说明。"},
            "multiline": {"type": "boolean", "description": "是否需要多行输入。"},
        },
        "required": ["scope", "title", "instructions"],
    },
)
async def request_secret_input(
    scope: str, title: str, instructions: str, multiline: bool = False
) -> dict:
    """Request a secret through the client UI and return only a handle."""
    return await request_secret_input_from_context(
        SecretInputRequest(scope, title, instructions, multiline)
    )


def _web_search_reuse_sources(config: AeroConfig, provider: str) -> list[str]:
    """Return model roles with a reusable same-provider key, without exposing it."""
    provider = normalize_provider_id(provider)
    sources: list[str] = []
    for saved_provider, profile in config.llm.providers.items():
        if normalize_provider_id(saved_provider) == provider and profile.api_key:
            sources.append("已配置的主模型供应商")
            break
    vision = resolved_vision_config(config)
    if (
        vision is not None
        and normalize_provider_id(vision.provider) == provider
        and vision.api_key
        and "已配置的视觉模型" not in sources
    ):
        sources.append("已配置的视觉模型")
    return sources


def _web_search_reuse_candidate(config: AeroConfig, provider: str) -> str:
    """Resolve a same-provider key only after the caller obtained authorization."""
    provider = normalize_provider_id(provider)
    for saved_provider, profile in config.llm.providers.items():
        if normalize_provider_id(saved_provider) == provider and profile.api_key:
            return profile.api_key
    vision = resolved_vision_config(config)
    if vision is not None and normalize_provider_id(vision.provider) == provider and vision.api_key:
        return vision.api_key
    return ""


def _save_web_search_credential(value: str) -> dict:
    """Consume a web-search-only key from the local secure input window."""
    key = value.strip()
    if not key:
        return {"status": "error", "message": "网页搜索 API Key 不能为空。"}
    config = find_config()
    provider = (config.web_search.provider or "bailian").strip().lower()
    if provider not in {"bailian", "zhipu"}:
        return {"status": "error", "message": f"不支持的网页搜索服务商：{provider}"}
    save_web_search_api_key(
        key,
        config.web_search.base_url,
        provider=provider,
        model=config.web_search.model,
    )
    config.web_search.api_key = key
    config.web_search.enabled = False
    config.web_search.mcp_verified = False
    config.save(find_config_path())
    return {
        "status": "success",
        "provider": provider,
        "api_key_configured": True,
        "mcp_verified": False,
        "message": (
            "网页搜索服务 API Key 已保存在用户级凭据中；尚未启用，"
            "请执行 /websearch on 进行连通性测试。"
        ),
    }


register_credential_spec(
    CredentialSpec(
        scope="web_search",
        title="输入网页搜索服务 API Key",
        instructions=(
            "这里只输入当前所选网页搜索供应商的 API Key。\n"
            "如需复用同一供应商已配置的 Key，必须先获得用户明确授权；不要自行复用。\n"
            "使用百炼时，还必须在百炼 MCP 广场手动开通 WebSearch（联网搜索）MCP。\n"
            "密钥只在本地保存，不会进入对话，也不会发送给模型。"
        ),
        multiline=False,
        consumer=_save_web_search_credential,
    )
)


@register_tool(
    name="check_web_search_config",
    description=(
        "检查网页搜索凭证状态，并返回百炼与智谱两种完整配置方法。"
        "用户要配置或修复网页搜索时调用；不要调用主模型 provider 配置工具，"
        "也不要在未经用户明确授权时自动复用模型 API Key。"
    ),
    parameters={"type": "object", "properties": {}},
)
def check_web_search_config() -> dict:
    config = find_config()
    provider = (config.web_search.provider or "bailian").strip().lower()
    if provider == "zhipu":
        name = "智谱 AI"
    else:
        provider = "bailian"
        name = "阿里云百炼"
    bailian_url = (
        "https://bailian.console.aliyun.com/cn-beijing/?apiKey=1&tab=globalset#/efm/api_key"
    )
    zhipu_url = "https://open.bigmodel.cn/apikey/platform"
    bailian_pricing_url = "https://help.aliyun.com/zh/model-studio/web-search-mcp"
    zhipu_pricing_url = "https://docs.bigmodel.cn/cn/guide/tools/web-search"
    configured = bool(config.web_search.api_key)
    bailian_reuse_sources = _web_search_reuse_sources(config, "bailian")
    zhipu_reuse_sources = _web_search_reuse_sources(config, "zhipu")
    reuse_available = {
        "bailian": bool(bailian_reuse_sources),
        "zhipu": bool(zhipu_reuse_sources),
    }
    if configured:
        configuration_state = "configured"
    elif any(reuse_available.values()):
        configuration_state = "reusable_key_available"
    else:
        configuration_state = "credential_required"
    reuse_guidance = ""
    if bailian_reuse_sources:
        reuse_guidance += (
            "\n检测到用户已经配置过百炼 API Key，可以复用，无需重新创建或输入。"
            "请把“明确授权复用现有百炼 API Key”作为百炼方案的首选项，"
            "并提醒用户仍需到百炼 MCP 广场开通 WebSearch（联网搜索）MCP。"
            "只有用户明确回复同意/授权复用后，才能调用 "
            'authorize_web_search_key_reuse(provider="bailian")；不得自动复用。\n'
        )
    if zhipu_reuse_sources:
        reuse_guidance += (
            "\n检测到用户已经配置过智谱 API Key，可以复用，无需重新输入。"
            "只有用户明确回复同意/授权复用后，才能调用 "
            'authorize_web_search_key_reuse(provider="zhipu")；不得自动复用。\n'
        )
    return {
        "provider": provider,
        "configuration_state": configuration_state,
        "api_key_configured": configured,
        "dedicated_web_search_key_configured": configured,
        "reusable_model_api_key_detected": any(reuse_available.values()),
        "mcp_verified": bool(config.web_search.mcp_verified),
        "message": (
            (
                f"当前选择的网页搜索供应商：{name}；网页搜索专用 Key 已配置。\n\n"
                if configured
                else (
                    "已检测到可复用的模型 API Key；不要再要求用户创建或输入同一"
                    "供应商的 Key。必须先说明可复用并取得用户明确授权。\n\n"
                    if any(reuse_available.values())
                    else f"当前选择的网页搜索供应商：{name}；尚无网页搜索凭证。\n\n"
                )
            )
            + "可选配置方法：\n"
            "1. 阿里云百炼 WebSearch MCP\n"
            f"   - 在 API-KEY 管理页创建或复制 DashScope API Key：{bailian_url}\n"
            "   - 登录百炼后进入 MCP 广场，搜索“WebSearch”或“联网搜索”，"
            "点击“立即开通”，再“确认开通”。\n"
            "   - API Key 和 MCP 服务开通两项缺一不可；同时检查账户余额和调用额度。\n"
            "   - 计费：全部用户前 2000 次调用免费；免费额度用尽后按 "
            "29 元/千次计费。价格可能调整，以阿里云官方计费页面为准。\n"
            "   - 如果要复用已配置的百炼 Key，必须由用户明确授权，系统不能自动复用。\n\n"
            "2. 智谱 AI 搜索\n"
            f"   - 在开放平台创建或复制 API Key：{zhipu_url}\n"
            "   - 无需开通百炼 MCP；请检查智谱账户余额和搜索调用额度。\n"
            "   - 计费（按搜索请求次数，不是按 token）：search_std 0.01 元/次；"
            "search_pro 0.03 元/次；search_pro_sogou、search_pro_quark 均为 0.05 元/次。"
            "Aero 默认使用 search_std。价格可能调整，以智谱官方联网搜索定价页为准。\n"
            "   - 如果要复用已配置的智谱 Key，同样必须由用户明确授权。\n\n"
            "如果尚未选定供应商，请先让用户选择百炼或智谱（也可执行 "
            "/websearch provider）。拿到 Key 后不要粘贴到聊天框；"
            "只回复“准备好了”，再调用 request_secret_input，由界面显示安全配置入口。"
            "\n无论是否检测到可复用的百炼 Key，都必须在同一条回复中完整展示"
            "“阿里云百炼”和“智谱 AI”两条方案，让用户自己选择；"
            "可复用百炼 Key 只能让百炼成为推荐方案，不能隐藏智谱方案。" + reuse_guidance
        ),
        "response_requirements": [
            "始终同时展示阿里云百炼与智谱 AI 两条完整方案",
            "检测到百炼 Key 时可把百炼标为推荐，但不得省略智谱方案",
            "写明百炼前 2000 次免费，之后 29 元/千次，并注明以官方页面为准",
            "写明智谱各搜索引擎按次计费、Aero 默认使用 search_std，并注明以官方页面为准",
            "复用任何已有 Key 前必须取得用户明确授权",
        ],
        "reuse_available": reuse_available,
        "reuse_sources": {
            "bailian": bailian_reuse_sources,
            "zhipu": zhipu_reuse_sources,
        },
        "providers": [
            {
                "id": "bailian",
                "name": "阿里云百炼 WebSearch MCP",
                "api_key_url": bailian_url,
                "requirements": [
                    "创建或复制 DashScope API Key",
                    "在百炼 MCP 广场手动开通 WebSearch（联网搜索）MCP",
                    "确认账户余额和调用额度可用",
                ],
                "pricing": {
                    "free_calls": 2000,
                    "price_cny_per_1000_calls_after_free_quota": 29,
                    "note": "价格可能调整，以阿里云官方计费页面为准",
                    "url": bailian_pricing_url,
                },
            },
            {
                "id": "zhipu",
                "name": "智谱 AI 搜索",
                "api_key_url": zhipu_url,
                "requirements": [
                    "创建或复制智谱 API Key",
                    "确认账户余额和搜索调用额度可用",
                ],
                "pricing": {
                    "billing_unit": "每次搜索请求",
                    "search_std_cny_per_call": 0.01,
                    "search_pro_cny_per_call": 0.03,
                    "search_pro_sogou_cny_per_call": 0.05,
                    "search_pro_quark_cny_per_call": 0.05,
                    "aero_default_engine": "search_std",
                    "note": "价格可能调整，以智谱官方联网搜索定价页为准",
                    "url": zhipu_pricing_url,
                },
            },
        ],
        "references": [bailian_pricing_url, zhipu_pricing_url],
    }


@register_tool(
    name="authorize_web_search_key_reuse",
    description=(
        "在用户明确回复同意/授权后，把同一供应商已配置的模型 API Key 复用为"
        "网页搜索凭证。未获得本轮明确授权时严禁调用；本工具不会返回密钥原文。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "enum": ["bailian", "zhipu"],
                "description": "用户明确授权复用的网页搜索供应商。",
            },
        },
        "required": ["provider"],
    },
)
def authorize_web_search_key_reuse(provider: str) -> dict:
    provider = provider.strip().lower()
    if provider not in {"bailian", "zhipu"}:
        return {"status": "error", "message": f"不支持的网页搜索服务商：{provider}"}
    config = find_config()
    key = _web_search_reuse_candidate(config, provider)
    if not key:
        return {
            "status": "error",
            "provider": provider,
            "message": "没有找到可复用的同供应商 API Key，请改用本地安全输入窗口配置。",
        }
    if provider == "zhipu":
        base_url = "https://open.bigmodel.cn/api/paas/v4/web_search"
        model = "search_std"
        action_required = (
            "凭证已授权复用。请执行 /websearch on 进行连通性测试；"
            "如失败，请检查智谱账户余额和搜索调用额度。"
        )
    else:
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = "qwen-turbo"
        action_required = (
            "凭证已授权复用，无需重新输入 Key。请先在百炼 MCP 广场搜索"
            "“WebSearch”或“联网搜索”，完成“立即开通 → 确认开通”，"
            "然后执行 /websearch on 进行连通性测试。"
        )
    save_web_search_api_key(
        key,
        base_url,
        provider=provider,
        model=model,
    )
    config.web_search.provider = provider
    config.web_search.api_key = key
    config.web_search.base_url = base_url
    config.web_search.model = model
    config.web_search.enabled = False
    config.web_search.mcp_verified = False
    config.web_search.mcp_verified_provider = ""
    config.web_search.mcp_verified_at = 0.0
    config.save(find_config_path())
    return {
        "status": "success",
        "provider": provider,
        "credential_reused": True,
        "api_key_configured": True,
        "mcp_verified": False,
        "action_required": action_required,
        "message": action_required,
    }


@register_tool(
    name="save_secret_handle",
    description=(
        "用一次性 credential_handle 在本地保存已注册用途的凭据。"
        "密钥原文不会进入模型上下文。先调用 request_secret_input，再调用本工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "scope": {"type": "string", "description": "凭据用途。"},
            "credential_handle": {
                "type": "string",
                "description": "request_secret_input 返回的一次性句柄。",
            },
        },
        "required": ["scope", "credential_handle"],
    },
)
def save_secret_handle(scope: str, credential_handle: str) -> dict:
    """Dispatch a one-time secret handle to the scope's local consumer."""
    return save_secret_from_context(scope, credential_handle)


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
            "3. 复制页面上的两行官方配置；不要把它们发到聊天框。\n"
            "4. 请用户只回复“准备好了”或“打开安全输入框”，再调用 request_secret_input，"
            "由界面显示安全配置入口。\n"
            "凭据不会发送给模型。"
        ),
    }


@register_tool(
    name="configure_cds_key",
    description=(
        "保存 CDS API 的 URL 和 Key 到用户级密钥文件。"
        "新流程接收 request_secret_input 返回的 credential_handle；也兼容旧的原始凭据文本。"
        "成功后用户即可下载数据。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "credential_handle": {
                "type": "string",
                "description": "由 request_secret_input 返回的一次性安全凭据句柄。",
            },
            "credential_string": {
                "type": "string",
                "description": "兼容旧调用；新流程不要传入原始密钥。",
            },
        },
    },
)
def configure_cds_key(credential_string: str = "", credential_handle: str = "") -> dict:
    """Parse CDS credential string and save to the user secrets file.

    Supports formats:
    - "https://cds.climate.copernicus.eu/api:xxxx-xxxx-xxxx-xxxx"
    - "url: https://cds.climate.copernicus.eu/api\nkey: xxxx-xxxx-xxxx-xxxx"
    """
    if credential_handle:
        return save_secret_from_context("cds", credential_handle)
    text = credential_string
    return _save_cds_credential(text)


def _save_cds_credential(text: str | None) -> dict:
    """Parse and save raw CDS input; called only within a local credential consumer."""
    if not text:
        return {
            "status": "error",
            "message": "未收到有效的安全凭据句柄，请先调用 request_secret_input。",
        }
    text = text.strip()

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


register_credential_spec(
    CredentialSpec(
        scope="cds",
        title="输入 CDS API 凭证",
        instructions=(
            "访问 https://cds.climate.copernicus.eu/ ，登录后进入 User Profile → API key。\n"
            "请粘贴 CDS 官方页面上的两行配置：\n"
            "url: https://cds.climate.copernicus.eu/api\n"
            "key: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
        ),
        multiline=True,
        consumer=_save_cds_credential,
    )
)


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
                "description": ("兼容旧调用；新流程不要传入原始密钥。"),
            },
            "credential_handle": {
                "type": "string",
                "description": "由 request_secret_input 返回的一次性安全凭据句柄。",
            },
        },
    },
)
def configure_ads_key(credential_string: str = "", credential_handle: str = "") -> dict:
    """Parse and save ADS API credentials."""
    if credential_handle:
        return save_secret_from_context("ads", credential_handle)
    text = credential_string
    return _save_ads_credential(text)


def _save_ads_credential(credential_string: str | None) -> dict:
    """Parse and save raw ADS input; called only within a local credential consumer."""
    ads_url, ads_key = _parse_ads_credentials(credential_string or "")
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


register_credential_spec(
    CredentialSpec(
        scope="ads",
        title="输入 ADS API 凭证",
        instructions=(
            "请粘贴 ADS 官方页面显示的 url/key 配置，或单独粘贴 Personal Access Token。\n"
            "内容只会在本地解析和保存，不会发送给模型。"
        ),
        multiline=True,
        consumer=_save_ads_credential,
    )
)


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
            "5. 使用 Aero 的本地安全凭据输入窗口粘贴 token；它不会发送给模型\n\n"
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
                "description": "兼容旧调用；新流程不要传入原始 token。",
            },
            "credential_handle": {
                "type": "string",
                "description": "由 request_secret_input 返回的一次性安全凭据句柄。",
            },
        },
    },
)
def configure_earthdata_token(token: str = "", credential_handle: str = "") -> dict:
    """Save a NASA Earthdata token in the user secrets file."""
    if credential_handle:
        return save_secret_from_context("earthdata", credential_handle)
    text = token
    return _save_earthdata_token(text)


def _save_earthdata_token(text: str | None) -> dict:
    """Normalize and save Earthdata input locally."""
    value = _normalize_secret_token(text or "")
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


register_credential_spec(
    CredentialSpec(
        scope="earthdata",
        title="输入 NASA Earthdata Token",
        instructions=(
            "请粘贴 NASA Earthdata 的 Access Token（可带 `Bearer ` 前缀）。\n"
            "它仅在本地保存，用于 MERRA-2/GES DISC 下载，不会发送给模型。"
        ),
        multiline=False,
        consumer=_save_earthdata_token,
    )
)


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
    config = AeroConfig.load(config_path) if config_path.exists() else AeroConfig.create_default()

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
    config = AeroConfig.load(config_path) if config_path.exists() else AeroConfig.create_default()

    had_key = bool(config.llm.active_api_key())
    previous_provider = config.llm.provider
    config.llm.set_active_api_key("")
    clear_llm_api_key(previous_provider)
    if reset_provider:
        preset = get_provider_preset("deepseek")
        config.llm.switch_provider("deepseek")
        config.llm.model = preset.default_model if preset else "deepseek-v4-flash"
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
        "清除用户级密钥文件中已保存的 ADS API 凭证。用户要求清除/删除 CAMS 或 ADS 凭证时调用。"
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
