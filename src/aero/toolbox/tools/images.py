"""Local image preparation tools for vision-model requests."""

from pathlib import Path

from PIL import Image, ImageOps

from aero.toolbox.paths import (
    find_project_dir,
    find_workspace_dir,
    resolve_project_path,
    short_path,
)
from aero.toolbox.registry import register_tool

_MIN_DIMENSION = 512
_MAX_DIMENSION = 4096
_MIN_TARGET_BYTES = 200 * 1024
_MAX_TARGET_BYTES = 5 * 1024 * 1024
_DEFAULT_MAX_DIMENSION = 1600
_DEFAULT_TARGET_BYTES = 1_000_000


@register_tool(
    name="prepare_image_for_vision",
    description=(
        "在本地生成一份适合视觉模型发送的压缩副本，不修改原图。"
        "当图片过大、分辨率过高或 analyze_image 超时时，必须先调用此工具，"
        "再把返回的 output_path 传给 analyze_image。默认最长边 1600 像素、目标小于约 1 MB。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": "项目或当前工作区内要压缩的图片路径。",
            },
            "max_dimension": {
                "type": "integer",
                "description": "输出图片最长边像素，默认 1600，范围 512–4096。",
            },
            "target_size_kb": {
                "type": "integer",
                "description": "输出 JPEG 的目标最大体积（KB），默认约 1000，范围 200–5120。",
            },
        },
        "required": ["image_path"],
    },
)
def prepare_image_for_vision(
    image_path: str,
    max_dimension: int = _DEFAULT_MAX_DIMENSION,
    target_size_kb: int = _DEFAULT_TARGET_BYTES // 1024,
) -> dict:
    """Create a resized JPEG copy for a vision request without touching the source."""
    if not _MIN_DIMENSION <= max_dimension <= _MAX_DIMENSION:
        return {
            "status": "error",
            "message": f"max_dimension 必须在 {_MIN_DIMENSION}–{_MAX_DIMENSION} 之间。",
        }

    target_bytes = target_size_kb * 1024
    if not _MIN_TARGET_BYTES <= target_bytes <= _MAX_TARGET_BYTES:
        return {
            "status": "error",
            "message": "target_size_kb 必须在 200–5120 之间。",
        }

    source = resolve_project_path(image_path).resolve()
    roots = {find_project_dir().resolve(), find_workspace_dir().resolve()}
    if not any(source.is_relative_to(root) for root in roots):
        return {"status": "error", "message": "只能处理当前项目或工作区内的图片。"}
    if not source.is_file():
        return {"status": "error", "message": f"图片文件不存在：{short_path(image_path)}"}

    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            original_size = image.size
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                background = Image.new("RGB", image.size, "white")
                alpha = image.getchannel("A") if "A" in image.getbands() else None
                background.paste(image, mask=alpha)
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")

            image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            output = _next_output_path(source)
            while True:
                for quality in (85, 75, 65, 55):
                    image.save(output, "JPEG", quality=quality, optimize=True, progressive=True)
                    if output.stat().st_size <= target_bytes:
                        break
                if output.stat().st_size <= target_bytes or max(image.size) <= _MIN_DIMENSION:
                    break
                image = image.resize(
                    tuple(max(1, round(side * 0.85)) for side in image.size),
                    Image.Resampling.LANCZOS,
                )
    except (OSError, ValueError) as exc:
        return {"status": "error", "message": f"图片压缩失败：{exc}"}

    return {
        "status": "success",
        "message": (
            "已生成视觉模型专用压缩副本；原图未修改。"
            "请把 output_path 作为 analyze_image 的 image_paths 再次分析。"
        ),
        "source_path": short_path(source),
        "output_path": short_path(output),
        "original_dimensions": list(original_size),
        "output_dimensions": list(image.size),
        "original_size_bytes": source.stat().st_size,
        "output_size_bytes": output.stat().st_size,
    }


def _next_output_path(source: Path) -> Path:
    """Return a new sibling path and never overwrite an existing image."""
    candidate = source.with_name(f"{source.stem}.vision.jpg")
    index = 2
    while candidate.exists():
        candidate = source.with_name(f"{source.stem}.vision-{index}.jpg")
        index += 1
    return candidate
