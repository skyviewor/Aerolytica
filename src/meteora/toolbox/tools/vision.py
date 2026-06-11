"""Vision model configuration and image analysis tools."""

from meteora.core.config import MeteoraConfig, save_vision_api_key, user_secrets_path
from meteora.core.debug_log import debug_exception
from meteora.toolbox.config_access import find_config, find_config_path
from meteora.toolbox.paths import short_path
from meteora.toolbox.registry import register_tool


def _ensure_vision_client():
    from meteora.agent.vision_client import VisionClient

    config = find_config()
    if not config.vision.model or not config.vision.api_key:
        return None, config
    return VisionClient(config.vision), config


_vision_usage: dict | None = None


def get_vision_usage() -> dict | None:
    return _vision_usage


def reset_vision_usage() -> None:
    global _vision_usage
    _vision_usage = None


def _vision_error_payload(
    exc: Exception,
    *,
    config,
    image_paths: list[str],
    detail: str,
) -> dict:
    error_type = getattr(exc, "error_type", exc.__class__.__name__)
    status_code = getattr(exc, "status_code", None)
    response_excerpt = getattr(exc, "response_excerpt", "")
    reason = str(exc).strip() or exc.__class__.__name__
    message = f"图片分析失败：{reason}"
    if status_code:
        message += f"（HTTP {status_code}）"
    if response_excerpt:
        message += f"\n\n服务返回摘要：{response_excerpt}"
    return {
        "status": "error",
        "message": message,
        "reason": reason,
        "error_type": error_type,
        "status_code": status_code,
        "response_excerpt": response_excerpt,
        "provider": config.vision.provider,
        "model": config.vision.model,
        "detail": detail,
        "image_paths": image_paths,
    }


@register_tool(
    name="check_vision_model_config",
    description=(
        "检查视觉模型是否已配置。用户询问「视觉模型配置了吗」、"
        "vision model 是否配置、图片分析模型状态时调用。"
        "只检查独立视觉模型配置，不检查主聊天 LLM 配置。"
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)
def check_vision_model_config() -> dict:
    config = find_config()
    api_key_url = "https://bailian.console.aliyun.com/cn-beijing?tab=model#/api-key"
    configured = bool(config.vision.api_key and config.vision.model)
    if configured:
        message = (
            f"视觉模型已配置：{config.vision.provider}/{config.vision.model}。"
            "它独立于主聊天模型，用于图片分析。"
        )
    else:
        message = (
            "视觉模型还没有配置。视觉模型独立于主聊天模型，"
            "运行在阿里云百炼 Qwen 上。\n\n"
            f"请到这里创建或复制 API key：{api_key_url}\n\n"
            "拿到后直接粘贴给我即可，我会在本地保存视觉模型配置。"
        )
    return {
        "status": "configured" if configured else "not_configured",
        "configured": configured,
        "provider": config.vision.provider,
        "model": config.vision.model,
        "api_key_configured": bool(config.vision.api_key),
        "api_key_url": api_key_url,
        "message": message,
    }


@register_tool(
    name="analyze_image",
    description=(
        "调用视觉模型分析图片。用于读取气象图表、卫星云图、雷达图、"
        "预报场可视化、多图对比等。支持单图或多图（多帧序列）。"
        "未配置视觉模型时会返回提示引导用户完成设置。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "图片文件路径列表。多图时可做对比分析。支持 png/jpg/webp/gif/bmp",
            },
            "prompt": {
                "type": "string",
                "description": "分析任务描述，如'描述500hPa高度场特征'或'对比两张图的差异'",
            },
            "detail": {
                "type": "string",
                "enum": ["low", "high", "auto"],
                "description": "分辨率级别。high 适合密集图表细读，auto 自适应。默认 high",
            },
            "force": {
                "type": "boolean",
                "description": "是否强制不走缓存重新分析。默认 false。当上次结果不佳时设为 true",
            },
        },
        "required": ["image_paths", "prompt"],
    },
)
async def analyze_image(
    image_paths: list[str],
    prompt: str,
    detail: str = "high",
    force: bool = False,
) -> dict:
    global _vision_usage
    _vision_usage = None
    from pathlib import Path

    from meteora.data.vision_cache import get as cache_get
    from meteora.data.vision_cache import put as cache_put

    client, config = _ensure_vision_client()
    if client is None:
        api_key_url = "https://bailian.console.aliyun.com/cn-beijing?tab=model#/api-key"
        return {
            "status": "not_configured",
            "message": (
                "视觉模型还没有配置，需要先创建阿里云百炼 API Key。\n\n"
                f"控制台入口：[阿里云百炼 API Key]({api_key_url})\n"
                f"如果你的终端不能点击链接，请复制这个地址打开：{api_key_url}\n\n"
                "登录后从左侧菜单栏进入 API Key 模块，点击「创建 API Key」，"
                "将生成的 Key 粘贴给我，我会帮你保存配置。"
            ),
            "setup_steps": [
                f"打开阿里云百炼控制台：{api_key_url}",
                "根据提示完成登录",
                "从左侧菜单栏进入 API Key 模块",
                "点击「创建 API Key」",
                "将生成的 Key 粘贴给我",
            ],
        }

    for path in image_paths:
        p = Path(path)
        if not p.exists():
            return {"status": "error", "message": f"图片文件不存在：{short_path(path)}"}

    if not force:
        cached = cache_get(image_paths, prompt, config.vision.model, config.vision.cache_ttl_hours)
        if cached:
            return {
                "status": "success",
                "analysis": cached,
                "model": config.vision.model,
                "cached": True,
            }

    try:
        result = await client.analyze(image_paths, prompt, detail)
        _vision_usage = client.last_usage
    except Exception as e:
        debug_exception(
            "vision.analyze_failed",
            e,
            provider=config.vision.provider,
            model=config.vision.model,
            image_paths=image_paths,
            detail=detail,
        )
        return _vision_error_payload(
            e,
            config=config,
            image_paths=image_paths,
            detail=detail,
        )

    cache_put(image_paths, prompt, config.vision.model, result)

    return {
        "status": "success",
        "analysis": result,
        "model": config.vision.model,
        "cached": False,
    }


@register_tool(
    name="configure_vision_model",
    description=(
        "保存视觉模型的 API Key 配置。用户提供 Key 后立即调用此工具保存。"
        "无需提前调用，仅当 analyze_image 返回未配置时使用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "api_key": {
                "type": "string",
                "description": "百炼 API Key",
            },
        },
        "required": ["api_key"],
    },
)
def configure_vision_model(api_key: str) -> dict:
    config_path = find_config_path()
    config = (
        MeteoraConfig.load(config_path) if config_path.exists() else MeteoraConfig.create_default()
    )

    config.vision.api_key = api_key
    config.vision.provider = "bailian"
    config.vision.model = "qwen3.7-plus"
    save_vision_api_key(api_key, config.vision.base_url)
    config.save(config_path)

    return {
        "status": "success",
        "message": (
            f"视觉模型已配置（{config.vision.provider}/{config.vision.model}），"
            f"API Key 已保存到 {user_secrets_path()}。现在可以使用 analyze_image 工具分析图片了。"
        ),
        "config_path": str(config_path),
        "secrets_path": str(user_secrets_path()),
    }
