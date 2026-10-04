"""General web search tool."""

from __future__ import annotations

from aero.core.config import resolved_vision_config
from aero.core.llm_providers import normalize_provider_id
from aero.data.web_search import (
    WEB_SEARCH_CONSOLE_URL,
    WEB_SEARCH_GUIDE_URL,
    check_bailian_web,
    search_bailian_web,
    search_zhipu_web,
)
from aero.toolbox.config_access import find_config
from aero.toolbox.registry import register_tool


async def check_web_search_status() -> dict:
    """Check web-search availability without submitting a search query."""
    config = find_config()
    provider = (config.web_search.provider or "bailian").strip().lower()
    if provider not in {"bailian", "zhipu"}:
        return {
            "available": False,
            "api_key_configured": False,
            "credential_reused": False,
            "error": f"不再支持联网搜索供应商：{provider}。当前仅支持阿里云百炼和智谱 AI。",
            "action_required": "在配置中选择阿里云百炼或智谱 AI",
        }
    api_key, credential_reused = _web_search_credential(config)
    if not api_key:
        reusable_sources = _reusable_model_key_sources(config, provider)
        if reusable_sources:
            return _reuse_authorization_required(provider, reusable_sources)
        return {
            "available": False,
            "api_key_configured": False,
            "credential_reused": False,
            "error": f"尚未配置{_provider_label(provider)} API Key，联网搜索暂不可用。",
            "message": (
                f"请先配置{_provider_label(provider)} API Key。"
                "百炼还需要在 MCP 广场开通“联网搜索 MCP”。"
            ),
            "action_required": "配置搜索服务凭证；若使用百炼，还需开通联网搜索 MCP",
            "references": _provider_references(provider),
        }

    if provider != "bailian":
        return {
            # A direct provider API key proves only that credentials were
            # entered; balance, quota, and service access are verified by the
            # first real search request.
            "available": False,
            "api_key_configured": True,
            "credential_reused": credential_reused,
            "credential_source": "视觉模型 API Key" if credential_reused else "联网搜索 API Key",
            "provider": (
                "阿里云百炼联网搜索" if provider == "bailian" else _provider_label(provider)
            ),
            "message": (
                f"已检测到{_provider_label(provider)} API Key；"
                "但尚未验证账户余额、搜索额度和服务开通状态，不能据此确认联网搜索可用。"
            ),
            "verification": "credential_present",
            "status_unknown": True,
            "action_required": (
                f"发起一次搜索进行验证；如失败，请登录{_provider_label(provider)} 开放平台，"
                "检查账户余额、搜索额度和服务开通状态"
            ),
            "references": _provider_references(provider),
        }

    try:
        tool_name = await check_bailian_web(api_key)
    except Exception as exc:
        result = _search_error(
            _exception_text(exc), credential_reused=credential_reused, provider=provider
        )
        result.update(
            available=False,
            message="当前无法确认联网搜索可用；请按提示检查百炼 WebSearch 服务状态。",
        )
        return result

    return {
        "available": True,
        "api_key_configured": True,
        "credential_reused": credential_reused,
        "credential_source": "视觉模型 API Key" if credential_reused else "联网搜索 API Key",
        "provider": "阿里云百炼联网搜索",
        "message": "联网搜索已就绪。",
        "service_tool": tool_name,
    }


@register_tool(
    name="check_web_search_status",
    description=(
        "检查当前是否真的可以联网搜索：验证已配置凭证和搜索服务状态；"
        "直接 API 供应商还会提示余额、额度和服务权限需首次搜索验证。"
        "不执行搜索。用户询问能否联网、联网搜索是否已配置或是否可用时必须调用。"
    ),
    parameters={"type": "object", "properties": {}},
)
async def check_web_search_status_tool() -> dict:
    """Tool entrypoint for checking live web-search availability."""
    return await check_web_search_status()


@register_tool(
    name="search_web",
    description=(
        "搜索互联网中的最新信息和普通网页，返回标题、摘要、发布日期和来源链接。"
        "适用于实时天气、台风、新闻、近期事件、网站资料和模型知识截止日期之后的信息。"
        "学术论文优先使用专门的学术文献检索能力。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "完整、明确的自然语言搜索问题或关键词",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回的搜索结果数，默认 8，范围 1 到 10",
                "minimum": 1,
                "maximum": 10,
            },
            "freshness_days": {
                "type": "integer",
                "description": "只关注最近多少天的内容；不限制时不要传",
                "minimum": 1,
                "maximum": 365,
            },
            "domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "优先限定的权威网站域名，最多 5 个，如 cma.gov.cn",
                "maxItems": 5,
            },
        },
        "required": ["query"],
    },
)
async def search_web(
    query: str,
    limit: int = 8,
    freshness_days: int | None = None,
    domains: list[str] | None = None,
) -> dict:
    """Search current web content using the configured Bailian credential."""
    config = find_config()

    provider = (config.web_search.provider or "bailian").strip().lower()
    if provider not in {"bailian", "zhipu"}:
        return {
            "found": False,
            "api_key_configured": False,
            "credential_reused": False,
            "error": f"不再支持联网搜索供应商：{provider}。当前仅支持阿里云百炼和智谱 AI。",
            "action_required": "在配置中选择阿里云百炼或智谱 AI",
        }
    api_key, credential_reused = _web_search_credential(config)
    if not api_key:
        reusable_sources = _reusable_model_key_sources(config, provider)
        if reusable_sources:
            return _reuse_authorization_required(provider, reusable_sources, search=True)
        return {
            "found": False,
            "api_key_configured": False,
            "credential_reused": False,
            "error": f"尚未配置{_provider_label(provider)} API Key，联网搜索暂不可用。",
            "message": (
                f"请先配置{_provider_label(provider)} API Key。"
                "百炼还需要在 MCP 广场开通“联网搜索 MCP”。"
            ),
            "action_required": "配置搜索服务凭证；若使用百炼，还需开通联网搜索 MCP",
            "references": _provider_references(provider),
        }

    try:
        kwargs = {"limit": limit, "freshness_days": freshness_days, "domains": domains}
        if provider == "zhipu":
            result = await search_zhipu_web(
                api_key, query, **kwargs, base_url=_provider_base_url(config, provider)
            )
        else:
            result = await search_bailian_web(api_key, query, **kwargs)
    except ValueError as exc:
        return {"found": False, "error": str(exc)}
    except Exception as exc:
        return _search_error(
            _exception_text(exc), credential_reused=credential_reused, provider=provider
        )

    from aero.data.pricing import record_service_cost

    # Directory prices as of 2026-07: Bailian WebSearch MCP ¥29/1K calls;
    # Zhipu search_std ¥0.01/call. Provider free quotas cannot be observed here.
    record_service_cost(
        f"web_search:{provider}",
        unit_price=0.029 if provider == "bailian" else 0.01,
    )
    result.update(
        {
            "query": query.strip(),
            "provider": (
                "阿里云百炼联网搜索" if provider == "bailian" else _provider_label(provider)
            ),
            "api_key_configured": True,
            "credential_reused": credential_reused,
            "credential_source": "视觉模型 API Key" if credential_reused else "联网搜索 API Key",
        }
    )
    if not result["found"]:
        result["message"] = "没有找到匹配的网页，请调整关键词或放宽时间和域名限制。"
    return result


def _web_search_credential(config) -> tuple[str, bool]:
    api_key = config.web_search.api_key.strip()
    if api_key:
        return api_key, False
    # Existing installations used the vision credential for search. Keep
    # that behaviour as a fallback while new setups store it separately.
    api_key = config.vision.api_key.strip()
    return api_key, bool(api_key)


def _reusable_model_key_sources(config, provider: str) -> list[str]:
    """Detect reusable provider credentials without returning their value.

    A model API key is deliberately not used by web search until the user has
    explicitly authorized that reuse.  Still reporting its presence here is
    essential: otherwise the assistant falsely tells the user that no Bailian
    key exists and sends them through an unnecessary key-creation flow.
    """
    provider = normalize_provider_id(provider)
    sources: list[str] = []
    for saved_provider, profile in config.llm.providers.items():
        if normalize_provider_id(saved_provider) == provider and profile.api_key:
            sources.append("主模型")
            break
    vision = resolved_vision_config(config)
    if (
        vision is not None
        and normalize_provider_id(vision.provider) == provider
        and vision.api_key
        and "视觉模型" not in sources
    ):
        sources.append("视觉模型")
    return sources


def _reuse_authorization_required(
    provider: str, sources: list[str], *, search: bool = False
) -> dict:
    """Describe an existing key that needs user authorization before reuse."""
    provider_name = _provider_label(provider)
    operation = "本次网页搜索" if search else "联网搜索"
    result = {
        "api_key_configured": False,
        "dedicated_web_search_key_configured": False,
        "reusable_model_api_key_detected": True,
        "reuse_available": True,
        "reuse_sources": sources,
        "credential_reused": False,
        "authorization_required": True,
        "provider": provider_name,
        "error": (
            f"已检测到{provider_name}的{'、'.join(sources)} API Key；"
            f"但尚未获得将它复用于{operation}的明确授权。"
        ),
        "message": (
            f"已检测到可复用的{provider_name}模型 API Key，无需重新创建或输入。"
            "必须同时向用户提供两条可选路径，而不是只推荐当前供应商：\n"
            "1. 阿里云百炼（可复用现有 Key）：用户明确回复“同意复用百炼 API Key”后，"
            "还需在百炼 MCP 广场开通 WebSearch/联网搜索（立即开通 → 确认开通），"
            "并检查余额与调用额度。\n"
            "2. 智谱 AI 搜索：用户也可以选择配置智谱 API Key；无需开通百炼 MCP，"
            "但需检查智谱账户余额和搜索额度。\n"
            "先让用户在“授权复用百炼”与“配置智谱”之间明确选择；"
            "只有选择百炼且明确授权后，才能调用 authorize_web_search_key_reuse。"
        ),
        "action_required": (
            "展示百炼与智谱两种完整方案并等待用户选择；"
            "若用户选择百炼且明确授权复用，调用 authorize_web_search_key_re"
            "use(provider='bailian')；"
            "若用户选择智谱，说明获取 Key 的地址并等待其准备好后打开安全输入窗口。"
        ),
        "alternative_provider_available": "zhipu" if provider == "bailian" else "bailian",
        "provider_options": ["bailian", "zhipu"],
        "references": _provider_references(provider),
    }
    if search:
        result["found"] = False
    else:
        result["available"] = False
    return result


def _provider_label(provider: str) -> str:
    return {
        "bailian": "阿里云百炼",
        "zhipu": "智谱 AI",
    }.get(provider, provider or "联网搜索服务")


def _provider_base_url(config, provider: str) -> str:
    configured = config.web_search.base_url.strip()
    defaults = {
        "zhipu": "https://open.bigmodel.cn/api/paas/v4/web_search",
    }
    if configured and not configured.startswith("https://dashscope.aliyuncs.com"):
        return configured
    return defaults.get(provider, configured)


def _provider_references(provider: str) -> list[str]:
    guides = {
        "zhipu": "https://open.bigmodel.cn/",
    }
    if provider in guides:
        return [guides[provider]]
    return [WEB_SEARCH_CONSOLE_URL, WEB_SEARCH_GUIDE_URL]


def _search_error(error: str, *, credential_reused: bool, provider: str = "bailian") -> dict:
    lowered = error.lower()
    billing_error = any(
        marker in lowered
        for marker in (
            "余额",
            "欠费",
            "充值",
            "quota",
            "insufficient",
            "credit",
            "billing",
            "payment",
            "balance",
            "额度用尽",
            "配额",
        )
    )
    if billing_error:
        if provider == "bailian":
            message = (
                "百炼联网搜索请求未完成，可能是账户余额不足、搜索额度用尽，"
                "或 WebSearch MCP 尚未开通。"
            )
            action_required = (
                "登录百炼开放平台检查账户余额和搜索额度，并在 MCP 广场确认已开通 WebSearch"
            )
        else:
            message = f"{_provider_label(provider)}联网搜索请求未完成，可能是账户余额不足、搜索额\
度用尽或服务未开通。"
            action_required = (
                f"登录{_provider_label(provider)} 开放平台，检查账户余额、搜索额度和服务开通状态"
            )
    elif "session terminated" in lowered or "401" in lowered or "403" in lowered:
        if provider == "bailian":
            message = (
                "已使用已配置的百炼 API Key，无需重新提供或配置 Key；"
                "但 WebSearch MCP 服务拒绝连接，请确认已在 MCP 广场开通服务。"
            )
            action_required = "在百炼 MCP 市场启用 WebSearch 并确认服务状态"
        else:
            message = (
                f"{_provider_label(provider)}拒绝了联网搜索请求，请检查 API Key、服务权限或余额。"
            )
            action_required = f"检查{_provider_label(provider)}的 API Key 和服务权限"
    elif "timeout" in lowered or "timed out" in lowered:
        message = "联网搜索请求超时，请稍后重试。"
        action_required = "稍后重试"
    else:
        message = f"{_provider_label(provider)}联网搜索暂时失败，请稍后重试或检查服务状态。"
        action_required = f"稍后重试或检查{_provider_label(provider)}服务状态"
    return {
        "found": False,
        "api_key_configured": True,
        "credential_reused": credential_reused,
        "credential_source": "视觉模型 API Key" if credential_reused else "联网搜索 API Key",
        "action_required": action_required,
        "error": message,
        "references": _provider_references(provider),
    }


def _exception_text(exception: BaseException) -> str:
    messages: list[str] = []

    def walk(current: BaseException) -> None:
        message = str(current).strip()
        if message and message not in messages:
            messages.append(message)
        children = getattr(current, "exceptions", ())
        for child in children:
            if isinstance(child, BaseException):
                walk(child)
        if current.__cause__ is not None:
            walk(current.__cause__)

    walk(exception)
    return " | ".join(messages)
