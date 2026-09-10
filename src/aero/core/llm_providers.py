"""Built-in OpenAI-compatible LLM provider presets."""

from __future__ import annotations

from dataclasses import dataclass


# Expose the official-account provider and login flows in the client UI.
OFFICIAL_ACCOUNT_UI_ENABLED = True


@dataclass(frozen=True)
class LLMProviderPreset:
    id: str
    name: str
    base_url: str
    default_model: str
    models: tuple[str, ...]
    api_key_url: str
    api_key_hint: str


OFFICIAL_LLM_PROVIDER = LLMProviderPreset(
    id="official",
    name="Aerolytica 官方",
    base_url="https://llm.aerolytica.skyviewor.team/v1",
    default_model="auto",
    models=("auto",),
    api_key_url="",
    api_key_hint="使用 Aerolytica 官方账户登录，无需配置 API Key。",
)


BUILTIN_LLM_PROVIDERS: dict[str, LLMProviderPreset] = {
    "bailian": LLMProviderPreset(
        id="bailian",
        name="阿里云百炼",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen3.7-plus",
        models=(
            "qwen3.7-max",
            "qwen3.7-plus",
            "qwen3.7-flash",
            "qwen3.6-plus",
            "qwen3.6-flash",
            "qwen3.5-plus",
            "qwen3.5-flash",
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "glm-5.2",
        ),
        api_key_url="https://bailian.console.aliyun.com/cn-beijing/?apiKey=1&tab=globalset#/efm/api_key",
        api_key_hint="打开阿里云百炼控制台，在 API-KEY 管理页面创建并复制 DashScope API key。",
    ),
    "deepseek": LLMProviderPreset(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        default_model="deepseek-v4-flash",
        models=(
            "deepseek-v4-flash",
            "deepseek-v4-pro",
        ),
        api_key_url="https://platform.deepseek.com/api_keys",
        api_key_hint="打开 DeepSeek 开放平台，在 API keys 页面创建并复制 sk- 开头的 key。",
    ),
    "kimi": LLMProviderPreset(
        id="kimi",
        name="Kimi",
        base_url="https://api.moonshot.cn/v1",
        default_model="kimi-k3",
        models=(
            "kimi-k3",
            "kimi-k2.7-code",
            "kimi-k2.6",
            "kimi-k2.5",
        ),
        api_key_url="https://platform.kimi.com/console/api-keys",
        api_key_hint="打开 Kimi 开放平台控制台，在 API Keys 页面创建并复制 Moonshot API key。",
    ),
}


@dataclass(frozen=True)
class ModelMetadata:
    """Short, user-facing capabilities for a known model."""

    summary: str
    supports_vision: bool = False


MODEL_METADATA: dict[tuple[str, str], ModelMetadata] = {
    ("official", "auto"): ModelMetadata("官方路由 · 自动"),
    ("deepseek", "deepseek-v4-flash"): ModelMetadata("文本 · 高性价比"),
    ("deepseek", "deepseek-v4-pro"): ModelMetadata("文本 · 旗舰"),
    ("bailian", "qwen3.7-max"): ModelMetadata("文本 · 旗舰"),
    ("bailian", "qwen3.7-plus"): ModelMetadata("多模态 · 均衡", supports_vision=True),
    ("bailian", "qwen3.7-flash"): ModelMetadata("多模态 · 轻量", supports_vision=True),
    ("bailian", "deepseek-v4-pro"): ModelMetadata("文本 · 高质量"),
    ("bailian", "deepseek-v4-flash"): ModelMetadata("文本 · 轻量"),
    ("bailian", "glm-5.2"): ModelMetadata("文本 · 高质量"),
    ("bailian", "qwen3.6-plus"): ModelMetadata("多模态 · 高质量", supports_vision=True),
    ("bailian", "qwen3.6-flash"): ModelMetadata("多模态 · 低成本", supports_vision=True),
    ("bailian", "qwen3.5-plus"): ModelMetadata("多模态 · 旧版", supports_vision=True),
    ("bailian", "qwen3.5-flash"): ModelMetadata("多模态 · 旧版", supports_vision=True),
    ("bailian", "qwen3-vl-plus"): ModelMetadata("多模态 · 高质量", supports_vision=True),
    ("bailian", "qwen3-vl-flash"): ModelMetadata("多模态 · 低成本", supports_vision=True),
    ("bailian", "qwen-vl-max"): ModelMetadata("多模态 · 旧版", supports_vision=True),
    ("bailian", "qwen-vl-plus"): ModelMetadata("多模态 · 旧版", supports_vision=True),
    ("kimi", "kimi-k3"): ModelMetadata("多模态 · 旗舰", supports_vision=True),
    ("kimi", "kimi-k2.7-code"): ModelMetadata("文本 · 代码"),
    ("kimi", "kimi-k2.6"): ModelMetadata("多模态 · 均衡", supports_vision=True),
    ("kimi", "kimi-k2.5"): ModelMetadata("多模态 · 旧版", supports_vision=True),
}


PROVIDER_ALIASES = {
    "aliyun": "bailian",
    "ali": "bailian",
    "dashscope": "bailian",
    "qwen": "bailian",
    "qwen3": "bailian",
    "qwen3.7": "bailian",
    "通义": "bailian",
    "通义千问": "bailian",
    "阿里云": "bailian",
    "阿里云百炼": "bailian",
    "百炼": "bailian",
    "moonshot": "kimi",
    "kimi": "kimi",
    "月之暗面": "kimi",
    "deepseek": "deepseek",
    "深度求索": "deepseek",
}

PROVIDER_MODEL_ALIASES = {
    "qwen3.7": ("bailian", "qwen3.7-plus"),
    "kimi-k2": ("kimi", "kimi-k3"),
    "k2": ("kimi", "kimi-k3"),
    "kimi-k3": ("kimi", "kimi-k3"),
    "k3": ("kimi", "kimi-k3"),
}


def normalize_provider_id(provider: str) -> str:
    value = provider.strip().lower()
    return PROVIDER_ALIASES.get(value, value)


def model_alias_for_provider(provider: str) -> tuple[str, str] | None:
    return PROVIDER_MODEL_ALIASES.get(provider.strip().lower())


def get_provider_preset(provider: str) -> LLMProviderPreset | None:
    provider_id = normalize_provider_id(provider)
    if provider_id == "official":
        return OFFICIAL_LLM_PROVIDER
    return BUILTIN_LLM_PROVIDERS.get(provider_id)


def provider_options() -> list[tuple[str, str]]:
    """Build labels for provider selection only.

    Model selection is a separate action, so showing a model here is
    misleading when a saved provider profile uses a different one.
    """
    options = [
        (preset.id, preset.name)
        for preset in BUILTIN_LLM_PROVIDERS.values()
    ]
    if OFFICIAL_ACCOUNT_UI_ENABLED:
        options.insert(0, (OFFICIAL_LLM_PROVIDER.id, OFFICIAL_LLM_PROVIDER.name))
    return options


def model_summary(provider: str, model: str) -> str:
    """Return a concise user-facing summary for a known model."""
    provider_id = normalize_provider_id(provider)
    metadata = MODEL_METADATA.get((provider_id, model.strip().lower()))
    return metadata.summary if metadata else "文本"


def model_tags(provider: str, model: str) -> tuple[str, str]:
    """Return aligned type and positioning labels for a known model."""
    summary = model_summary(provider, model)
    model_type, _, positioning = summary.partition(" · ")
    return model_type, positioning


def model_supports_vision(provider: str, model: str) -> bool:
    """Return whether a built-in model is known to accept image messages."""
    provider_id = normalize_provider_id(provider)
    model_id = model.strip().lower()
    metadata = MODEL_METADATA.get((provider_id, model_id))
    if metadata is not None:
        return metadata.supports_vision
    if provider_id == "openai":
        return model_id.startswith("gpt-4o") or model_id.startswith("gpt-4.1")
    if provider_id == "bailian":
        return "vl" in model_id
    return False
