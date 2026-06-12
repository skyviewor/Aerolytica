"""Built-in OpenAI-compatible LLM provider presets."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMProviderPreset:
    id: str
    name: str
    base_url: str
    default_model: str
    models: tuple[str, ...]
    api_key_url: str
    api_key_hint: str


BUILTIN_LLM_PROVIDERS: dict[str, LLMProviderPreset] = {
    "deepseek": LLMProviderPreset(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        default_model="deepseek-v4-flash",
        models=(
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-chat",
            "deepseek-reasoner",
        ),
        api_key_url="https://platform.deepseek.com/api_keys",
        api_key_hint="打开 DeepSeek 开放平台，在 API keys 页面创建并复制 sk- 开头的 key。",
    ),
    "bailian": LLMProviderPreset(
        id="bailian",
        name="阿里云百炼",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
        models=(
            "qwen3.7",
            "qwen-plus",
            "qwen-max",
            "qwen-turbo",
            "qwen-long",
            "qwen3-max",
            "qwen3-plus",
        ),
        api_key_url="https://bailian.console.aliyun.com/",
        api_key_hint="打开阿里云百炼控制台，在 API-KEY 管理页面创建并复制 DashScope API key。",
    ),
    "kimi": LLMProviderPreset(
        id="kimi",
        name="Kimi",
        base_url="https://api.moonshot.cn/v1",
        default_model="kimi-k2.6",
        models=(
            "kimi-k2.6",
            "kimi-k2.5",
            "kimi-k2-thinking",
            "kimi-k2-0905-preview",
            "moonshot-v1-128k",
            "moonshot-v1-32k",
        ),
        api_key_url="https://platform.moonshot.cn/console/api-keys",
        api_key_hint="打开 Kimi 开放平台控制台，在 API Keys 页面创建并复制 Moonshot API key。",
    ),
    "openai": LLMProviderPreset(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o",
        models=("gpt-4o", "gpt-4o-mini"),
        api_key_url="https://platform.openai.com/api-keys",
        api_key_hint="打开 OpenAI Platform，在 API keys 页面创建并复制 key。",
    ),
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
    "qwen3.7": ("bailian", "qwen3.7"),
    "kimi-k2": ("kimi", "kimi-k2.6"),
    "k2": ("kimi", "kimi-k2.6"),
    "kimi-thinking": ("kimi", "kimi-k2-thinking"),
    "kimi-k2-thinking": ("kimi", "kimi-k2-thinking"),
}


def normalize_provider_id(provider: str) -> str:
    value = provider.strip().lower()
    return PROVIDER_ALIASES.get(value, value)


def model_alias_for_provider(provider: str) -> tuple[str, str] | None:
    return PROVIDER_MODEL_ALIASES.get(provider.strip().lower())


def get_provider_preset(provider: str) -> LLMProviderPreset | None:
    return BUILTIN_LLM_PROVIDERS.get(normalize_provider_id(provider))


def provider_options() -> list[tuple[str, str]]:
    return [
        (preset.id, f"{preset.name}    {preset.default_model}")
        for preset in BUILTIN_LLM_PROVIDERS.values()
    ]
