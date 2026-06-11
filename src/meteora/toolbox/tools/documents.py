"""PDF extraction and system image preview tools."""

import os
import subprocess
import sys
from pathlib import Path

from meteora.toolbox.file_access import READ_FILES
from meteora.toolbox.paths import find_project_dir, short_path
from meteora.toolbox.registry import register_tool


@register_tool(
    name="read_pdf",
    description=(
        "提取 PDF 文件的文本内容、表格和元信息，适合阅读论文、报告等 PDF 文档。"
        "返回全文文本和所有表格（含表头、行数据）。"
        "如果返回的 has_text 为 false，可能是扫描版 PDF 无法直接提取文本。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "PDF 文件的绝对路径",
            },
        },
        "required": ["file_path"],
    },
)
async def read_pdf(file_path: str) -> dict:
    """Extract text and tables from a PDF file."""
    from meteora.data.pdf_reader import extract_text_async

    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"文件不存在: {short_path(file_path)}"}
    if not path.suffix.lower() == ".pdf":
        return {"status": "error", "message": f"文件不是 PDF: {short_path(file_path)}"}

    READ_FILES.add(file_path)

    try:
        result = await extract_text_async(path)
    except Exception as e:
        return {"status": "error", "message": f"PDF 解析失败: {e}"}

    result["status"] = "success"
    result["file_path"] = short_path(path)
    return result


@register_tool(
    name="preview_image",
    description=(
        "用系统默认图片查看器打开预览一张图片。当用户说'帮我打开这张图'、"
        "'让我看看这个图'，或你生成图片后用户想查看时，直接调用此工具，"
        "不用让用户自己输 /preview 命令。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "图片文件的相对路径，如 figures/plot.png",
            },
        },
        "required": ["file_path"],
    },
)
def preview_image(file_path: str) -> dict:
    path = Path(file_path)
    if not path.is_absolute():
        path = find_project_dir() / path
    if not path.exists():
        return {"status": "error", "message": f"图片文件不存在: {short_path(file_path)}"}

    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=True)
        elif sys.platform.startswith("win"):
            os.startfile(str(path))
        else:
            subprocess.run(["xdg-open", str(path)], check=True)
        return {
            "status": "success",
            "message": f"已打开图片: {short_path(path)}",
            "file_path": short_path(path),
        }
    except Exception as e:
        return {"status": "error", "message": f"无法打开图片: {e}"}
