"""Export the fixed Markdown paper body with Pandoc."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aero.paper_versions import PaperVersionManager


class PaperExportError(RuntimeError):
    """Raised when a paper export cannot be completed."""

    def __init__(self, message: str, *, missing_tools: list[str] | None = None):
        super().__init__(message)
        self.missing_tools = list(missing_tools or [])


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


EXPORT_EXTENSIONS = {"latex": "tex", "word": "docx", "pdf": "pdf"}


def export_paper(
    project_dir: str | Path,
    output_format: str,
    *,
    pandoc_path: str | None = None,
    tectonic_path: str | None = None,
    env: dict[str, str] | None = None,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    """Convert ``paper/main.md`` to LaTeX, Word, or PDF atomically."""
    normalized_format = str(output_format).strip().lower()
    extension = EXPORT_EXTENSIONS.get(normalized_format)
    if extension is None:
        raise PaperExportError("导出格式只支持 latex、word 或 pdf。")
    project = Path(project_dir).resolve()
    source = project / PaperVersionManager.DOCUMENT
    output = project / "paper" / f"main.{extension}"
    if not source.is_file():
        raise PaperExportError("论文正文不存在，请先执行 /paper init。")

    runtime_env = dict(env or os.environ)
    pandoc = pandoc_path or shutil.which("pandoc", path=runtime_env.get("PATH"))
    missing_tools = []
    if not pandoc:
        missing_tools.append("pandoc")
    tectonic = None
    if normalized_format == "pdf":
        tectonic = tectonic_path or shutil.which("tectonic", path=runtime_env.get("PATH"))
        if not tectonic:
            missing_tools.append("tectonic")
    if missing_tools:
        labels = "、".join(tool.title() for tool in missing_tools)
        raise PaperExportError(
            f"缺少 {labels}，无法导出论文。",
            missing_tools=missing_tools,
        )

    temporary = output.with_name(f".main.tmp-{uuid.uuid4().hex[:8]}.{extension}")
    command = [
        str(pandoc),
        str(source),
        "--from=markdown+tex_math_dollars+pipe_tables+footnotes",
        f"--resource-path={source.parent}",
        f"--output={temporary}",
    ]
    if normalized_format == "latex":
        command.extend(["--to=latex", "--standalone", "--wrap=preserve"])
    elif normalized_format == "word":
        command.extend(["--to=docx", "--standalone"])
    else:
        command.extend([f"--pdf-engine={tectonic}", "--standalone"])
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            env=runtime_env,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "未知错误").strip()
            raise PaperExportError(f"Pandoc 导出失败：{detail}")
        if not temporary.is_file():
            raise PaperExportError(f"Pandoc 未生成 {extension} 文件。")
        temporary.replace(output)
    except subprocess.TimeoutExpired as exc:
        raise PaperExportError("Pandoc 导出超时。") from exc
    except OSError as exc:
        raise PaperExportError(f"无法启动 Pandoc：{exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()

    return {
        "source": source.relative_to(project).as_posix(),
        "output": output.relative_to(project).as_posix(),
        "size": output.stat().st_size,
        "format": normalized_format,
    }


def export_paper_latex(
    project_dir: str | Path,
    **kwargs: Any,
) -> dict[str, Any]:
    """Backward-compatible wrapper for LaTeX export."""
    return export_paper(project_dir, "latex", **kwargs)
