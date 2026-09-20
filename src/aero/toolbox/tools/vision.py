"""Vision-model configuration and image-analysis tools."""

from contextvars import ContextVar

from aero.core.config import (
    AeroConfig,
    resolved_vision_config,
    save_vision_api_key,
    user_secrets_path,
    vision_is_configured,
)
from aero.core.debug_log import debug_exception
from aero.toolbox.config_access import find_config, find_config_path
from aero.toolbox.paths import short_path
from aero.toolbox.registry import register_tool


def _ensure_vision_client():
    config = find_config()
    vision_config = resolved_vision_config(config)
    if vision_config is None:
        return None, config, None
    if config.llm.provider == "official" and vision_config.mode == "official":
        from aero.agent.official_vision_client import OfficialVisionClient

        return OfficialVisionClient(vision_config), config, vision_config
    from aero.agent.vision_client import VisionClient

    return VisionClient(vision_config), config, vision_config


_vision_usage: ContextVar[dict | None] = ContextVar("aero_vision_usage", default=None)


def get_vision_usage() -> dict | None:
    return _vision_usage.get()


def reset_vision_usage() -> None:
    _vision_usage.set(None)


def _vision_error_payload(exc: Exception, *, config, image_paths: list[str], detail: str) -> dict:
    error_type = getattr(exc, "error_type", exc.__class__.__name__)
    status_code = getattr(exc, "status_code", None)
    response_excerpt = getattr(exc, "response_excerpt", "")
    reason = str(exc).strip() or exc.__class__.__name__
    message = f"图片分析失败：{reason}"
    if error_type == "timeout":
        message += (
            "\n\n请先调用 prepare_image_for_vision 生成压缩副本，"
            "再用返回的 output_path 重试图片分析。"
        )
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
        "provider": config.provider,
        "model": config.model,
        "detail": detail,
        "image_paths": image_paths,
    }


@register_tool(
    name="check_vision_model_config",
    description="检查视觉模型是否已配置，包含独立视觉模型或复用主模型能力的模式。",
    parameters={"type": "object", "properties": {}},
)
def check_vision_model_config() -> dict:
    config = find_config()
    configured = vision_is_configured(config)
    mode = config.vision.mode
    if configured and mode == "reuse_primary":
        resolved = resolved_vision_config(config)
        assert resolved is not None
        provider, model = resolved.provider, resolved.model
        message = f"视觉能力已配置：复用已保存的视觉模型 {provider}/{model}。"
    elif configured:
        provider, model = config.vision.provider, config.vision.model
        message = f"视觉模型已配置：{provider}/{model}。"
    else:
        provider, model = config.vision.provider, config.vision.model
        message = (
            "尚未配置视觉模型。请输入 /vision 打开安全的配置界面，选择或配置视觉模型；"
            "也可以在首次引导中选择复用支持多模态的主模型。"
        )
    return {
        "status": "configured" if configured else "not_configured",
        "configured": configured,
        "mode": mode,
        "provider": provider,
        "model": model,
        "message": message,
    }


@register_tool(
    name="analyze_image",
    description=(
        "调用视觉模型分析图片，用于气象图表、卫星云图、雷达图、预报场可视化和多图对比。"
        "未配置视觉能力时会返回提示，客户端将引导用户安全配置。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "image_paths": {"type": "array", "items": {"type": "string"}},
            "prompt": {"type": "string"},
            "detail": {"type": "string", "enum": ["low", "high", "auto"]},
            "force": {"type": "boolean"},
        },
        "required": ["image_paths", "prompt"],
    },
)
async def analyze_image(
    image_paths: list[str], prompt: str, detail: str = "high", force: bool = False
) -> dict:
    _vision_usage.set(None)
    from pathlib import Path

    from aero.data.vision_cache import get as cache_get
    from aero.data.vision_cache import put as cache_put

    client, _, vision_config = _ensure_vision_client()
    if client is None or vision_config is None:
        return {
            "status": "not_configured",
            "message": (
                "当前任务需要视觉模型，但尚未配置视觉能力。请输入 /vision 打开安全配置界面，"
                "选择或配置视觉模型；可复用支持多模态的主模型，或配置独立视觉模型。"
            ),
            "setup_required": "vision",
        }

    for path in image_paths:
        if not Path(path).exists():
            return {"status": "error", "message": f"图片文件不存在：{short_path(path)}"}

    if not force:
        cached = cache_get(image_paths, prompt, vision_config.model, vision_config.cache_ttl_hours)
        if cached:
            return {
                "status": "success",
                "analysis": cached,
                "model": vision_config.model,
                "cached": True,
            }

    try:
        result = await client.analyze(image_paths, prompt, detail)
        _vision_usage.set(client.last_usage)
    except Exception as exc:
        debug_exception(
            "vision.analyze_failed",
            exc,
            provider=vision_config.provider,
            model=vision_config.model,
            image_paths=image_paths,
            detail=detail,
        )
        return _vision_error_payload(
            exc,
            config=vision_config,
            image_paths=image_paths,
            detail=detail,
        )
    finally:
        await client.close()

    cache_put(image_paths, prompt, vision_config.model, result)
    return {"status": "success", "analysis": result, "model": vision_config.model, "cached": False}


@register_tool(
    name="configure_vision_model",
    description="保存独立视觉模型的 API Key 配置。",
    parameters={
        "type": "object",
        "properties": {"api_key": {"type": "string"}},
        "required": ["api_key"],
    },
)
def configure_vision_model(api_key: str) -> dict:
    config_path = find_config_path()
    config = AeroConfig.load(config_path) if config_path.exists() else AeroConfig.create_default()
    config.vision.mode = "separate"
    config.vision.provider = "bailian"
    config.vision.model = "qwen3.7-plus"
    save_vision_api_key(
        api_key,
        config.vision.base_url,
        provider=config.vision.provider,
        model=config.vision.model,
        mode="separate",
    )
    config.save(config_path)
    return {
        "status": "success",
        "message": f"视觉模型已配置（{config.vision.provider}/{config.vision.model}）。",
        "config_path": str(config_path),
        "secrets_path": str(user_secrets_path()),
    }
