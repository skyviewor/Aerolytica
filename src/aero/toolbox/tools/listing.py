"""Local data-file and generated-figure listing tools."""

from datetime import datetime
from pathlib import Path

from aero.toolbox.download_progress import format_size
from aero.toolbox.paths import find_project_dir, resolve_project_path, short_path
from aero.toolbox.registry import register_tool


@register_tool(
    name="list_files",
    description="列出指定目录下的文件，按文件类型过滤。用于查看已下载的数据文件。",
    parameters={
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": "要查看的目录路径，如 'my-aero-test/data'",
            },
            "pattern": {
                "type": "string",
                "description": "文件类型过滤，如 'nc' 只列 NetCDF 文件，不填则列所有文件",
            },
        },
        "required": ["directory"],
    },
)
async def list_files(directory: str, pattern: str = "") -> dict:
    """List files in a directory, optionally filtered by extension."""
    p = resolve_project_path(directory)
    if not p.exists():
        return {"status": "error", "message": f"目录不存在: {short_path(p)}"}
    if not p.is_dir():
        return {"status": "error", "message": f"不是目录: {short_path(p)}"}

    files = []
    try:
        for entry in sorted(p.iterdir()):
            if entry.is_file():
                if pattern and not entry.name.endswith(pattern):
                    continue
                stat = entry.stat()
                files.append(
                    {
                        "name": entry.name,
                        "path": short_path(entry),
                        "size": stat.st_size,
                        "size_human": format_size(stat.st_size),
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    }
                )
    except PermissionError as e:
        return {"status": "error", "message": f"无法访问目录: {e}"}

    return {
        "directory": short_path(p),
        "file_count": len(files),
        "files": files,
    }


_FIGURE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"}


@register_tool(
    name="list_figures",
    description=(
        "列出项目 figures/ 目录中的图片文件。"
        "当用户问「有哪些图片」「查看现在的图」「列出生成的图片」等需求时使用。"
        "只检查 figures/，不要扫描 data/ 或其他目录。"
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)
async def list_figures() -> dict:
    """List image files from the project figures directory only."""
    project_dir = find_project_dir()
    figures_dir = project_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    files = []
    for entry in sorted(figures_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not entry.is_file() or entry.suffix.lower() not in _FIGURE_EXTENSIONS:
            continue
        stat = entry.stat()
        width, height = _image_dimensions(entry)
        files.append(
            {
                "name": entry.name,
                "path": short_path(entry),
                "relative_path": str(entry.relative_to(project_dir)),
                "size": stat.st_size,
                "size_human": format_size(stat.st_size),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "width": width,
                "height": height,
            }
        )

    return {
        "status": "success",
        "directory": short_path(figures_dir),
        "relative_directory": "figures",
        "file_count": len(files),
        "files": files,
    }


def _image_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as image:
            return image.size
    except Exception:
        return None, None


@register_tool(
    name="delete_file",
    description=(
        "删除指定的本地文件。此操作不可撤销，执行前需要用户确认。用于清理不再需要的数据文件。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要删除的文件完整路径",
            },
        },
        "required": ["file_path"],
    },
    requires_confirmation=True,
)
async def delete_file(file_path: str) -> dict:
    """Delete a local file after confirmation."""
    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"文件不存在: {short_path(file_path)}"}
    if not path.is_file():
        return {"status": "error", "message": f"不是文件: {short_path(file_path)}"}

    try:
        file_size = path.stat().st_size
        path.unlink()
    except PermissionError as e:
        return {"status": "error", "message": f"权限不足: {e}"}
    except OSError as e:
        return {"status": "error", "message": f"删除失败: {e}"}

    return {
        "status": "success",
        "message": f"已删除: {short_path(path)}",
        "file_path": short_path(path),
        "file_size": file_size,
    }
