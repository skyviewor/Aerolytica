"""Local text-file reading and editing tools."""

from pathlib import Path

from meteora.toolbox.file_access import READ_FILES, is_in_project
from meteora.toolbox.paths import resolve_project_path, short_path
from meteora.toolbox.registry import register_tool


@register_tool(
    name="read_file",
    description=(
        "读取本地文件或目录。文件返回带行号的内容（格式：行号: 内容），"
        "目录返回条目列表。编辑文件前必须先用此工具读取文件。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文件或目录的绝对路径",
            },
            "offset": {
                "type": "integer",
                "description": "起始行号（1-indexed），默认从第 1 行开始",
            },
            "limit": {
                "type": "integer",
                "description": "最大读取行数，默认 2000",
            },
        },
        "required": ["file_path"],
    },
)
async def read_file(file_path: str, offset: int = 1, limit: int = 2000) -> dict:
    """Read a file or directory."""
    path = resolve_project_path(file_path)
    resolved_file_path = str(path)
    if not path.exists():
        return {"status": "error", "message": f"路径不存在: {short_path(file_path)}"}

    READ_FILES.add(resolved_file_path)

    if path.is_dir():
        entries = []
        for entry in sorted(path.iterdir()):
            suffix = "/" if entry.is_dir() else ""
            entries.append(f"{entry.name}{suffix}")
        return {
            "type": "directory",
            "path": short_path(path),
            "entries": entries,
            "count": len(entries),
        }

    try:
        with open(path) as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        return {
            "type": "file",
            "path": short_path(path),
            "binary": True,
            "size": path.stat().st_size,
            "message": "二进制文件，无法文本读取",
        }
    except Exception as e:
        return {"status": "error", "message": f"读取失败: {e}"}

    start = max(0, offset - 1)
    end = min(len(lines), start + limit)
    content_lines = []
    for i in range(start, end):
        line = lines[i].rstrip("\n\r")
        content_lines.append(f"{i + 1}: {line}")

    return {
        "type": "file",
        "path": short_path(path),
        "lines_total": len(lines),
        "lines_returned": len(content_lines),
        "offset": start + 1,
        "content": content_lines,
    }


@register_tool(
    name="write_file",
    description=(
        "创建或覆盖文件。如果文件已存在，必须先调用 read_file 读取。"
        "只在项目目录下写文件；超出范围的路径需要用户确认。"
        "不要自动创建 README、docs 等文档文件。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文件的完整绝对路径",
            },
            "content": {
                "type": "string",
                "description": "要写入的文件内容",
            },
        },
        "required": ["file_path", "content"],
    },
)
async def write_file(file_path: str, content: str) -> dict:
    """Write content to a file."""
    path = resolve_project_path(file_path)
    resolved_file_path = str(path)

    if path.exists():
        if resolved_file_path not in READ_FILES:
            return {
                "status": "error",
                "message": (
                    "文件已存在，但尚未用 read_file 读取。"
                    f"请先调用 read_file 读取 {short_path(file_path)}。"
                ),
            }
        if path.is_dir():
            return {"status": "error", "message": f"路径是目录，不能写入: {file_path}"}

    if not is_in_project(path):
        return {
            "status": "confirm_required",
            "message": f"文件路径 {short_path(file_path)} 不在项目目录下。",
            "file_path": file_path,
        }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    except Exception as e:
        return {"status": "error", "message": f"写入失败: {e}"}

    READ_FILES.add(resolved_file_path)
    return {
        "status": "success",
        "message": f"已写入 {short_path(path)}（{len(content)} 字符, {content.count(chr(10))} 行）",
        "file_path": short_path(path),
    }


@register_tool(
    name="edit_file",
    description=(
        "精确替换文件中的文本。先用 read_file 读取文件，"
        "从输出中复制原文作为 old_string（保留缩进格式），"
        "填入新文本作为 new_string。\n\n"
        "如果 old_string 匹配多次会报错——此时扩大上下文或设 replace_all=true。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要编辑的文件绝对路径",
            },
            "old_string": {
                "type": "string",
                "description": "要被替换的原文（精确匹配，含缩进）",
            },
            "new_string": {
                "type": "string",
                "description": "替换后的文本",
            },
            "replace_all": {
                "type": "boolean",
                "description": "替换所有匹配项（默认只替换第一处）",
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    },
)
async def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> dict:
    """Edit a file by exact string replacement."""
    path = resolve_project_path(file_path)
    resolved_file_path = str(path)

    if resolved_file_path not in READ_FILES:
        return {
            "status": "error",
            "message": f"尚未用 read_file 读取 {file_path}。编辑前必须先读文件。",
        }

    if not is_in_project(path):
        return {
            "status": "confirm_required",
            "message": f"文件路径 {short_path(file_path)} 不在项目目录下。",
            "file_path": file_path,
        }

    try:
        with open(path) as f:
            original = f.read()
    except Exception as e:
        return {"status": "error", "message": f"读取失败: {e}"}

    occurrences = original.count(old_string)
    if occurrences == 0:
        return {
            "status": "error",
            "message": "在文件中未找到 old_string。请检查原文是否精确匹配（含空格/缩进）。",
        }

    if occurrences > 1 and not replace_all:
        return {
            "status": "error",
            "message": (
                f"找到 {occurrences} 处匹配。"
                "请扩大上下文使 old_string 更唯一，或设 replace_all=true。"
            ),
        }

    new_content = (
        original.replace(old_string, new_string)
        if replace_all
        else original.replace(old_string, new_string, 1)
    )

    try:
        with open(path, "w") as f:
            f.write(new_content)
    except Exception as e:
        return {"status": "error", "message": f"写入失败: {e}"}

    READ_FILES.add(resolved_file_path)
    return {
        "status": "success",
        "message": f"已修改 {short_path(path)}（{'所有' if replace_all else '1'} 处替换）",
        "file_path": short_path(path),
        "occurrences": occurrences,
        "replaced": occurrences if replace_all else 1,
    }
