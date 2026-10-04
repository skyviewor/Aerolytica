"""Tests for fixed Markdown-to-LaTeX paper export."""

import subprocess

import pytest

from aero.cli.main import AeroApp
from aero.core.config import AeroConfig
from aero.data.modes import is_tool_allowed
from aero.paper_export import PaperExportError, export_paper, export_paper_latex
from aero.toolbox.runtime_manager import RUNTIME_TOOL_PACKAGES


def _project_with_paper(tmp_path, markdown="# Title\n\nResult $x^2$.\n"):
    source = tmp_path / "paper" / "main.md"
    source.parent.mkdir()
    source.write_text(markdown, encoding="utf-8")
    return source


def test_export_paper_latex_uses_pandoc_and_atomically_writes_main_tex(tmp_path):
    source = _project_with_paper(tmp_path)
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        output_arg = next(item for item in command if item.startswith("--output="))
        output = output_arg.split("=", 1)[1]
        with open(output, "w", encoding="utf-8") as stream:
            stream.write("\\documentclass{article}\n\\begin{document}\nResult $x^2$.\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = export_paper_latex(tmp_path, pandoc_path="/fake/pandoc", runner=runner)

    output = tmp_path / "paper" / "main.tex"
    assert result["source"] == "paper/main.md"
    assert result["output"] == "paper/main.tex"
    assert output.read_text().startswith("\\documentclass")
    assert captured["command"][1] == str(source)
    assert "--standalone" in captured["command"]
    assert "--to=latex" in captured["command"]
    assert not list(output.parent.glob(".main.tex.tmp-*"))


def test_export_failure_keeps_previous_latex_file(tmp_path):
    _project_with_paper(tmp_path)
    output = tmp_path / "paper" / "main.tex"
    output.write_text("old latex\n", encoding="utf-8")

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 2, "", "bad markdown")

    with pytest.raises(PaperExportError, match="bad markdown"):
        export_paper_latex(tmp_path, pandoc_path="/fake/pandoc", runner=runner)

    assert output.read_text() == "old latex\n"


def test_export_requires_fixed_source_and_pandoc(tmp_path, monkeypatch):
    with pytest.raises(PaperExportError, match="/paper init"):
        export_paper_latex(tmp_path, pandoc_path="/fake/pandoc")

    _project_with_paper(tmp_path)
    monkeypatch.setattr("aero.paper_export.shutil.which", lambda *args, **kwargs: None)
    with pytest.raises(PaperExportError, match="缺少 Pandoc"):
        export_paper_latex(tmp_path, env={"PATH": ""})


def test_pandoc_is_managed_and_export_is_execute_only():
    assert RUNTIME_TOOL_PACKAGES["pandoc"] == ("pandoc", ["pandoc"])
    assert RUNTIME_TOOL_PACKAGES["tectonic"] == ("tectonic", ["tectonic"])
    assert is_tool_allowed("export_paper", "execute") is True
    assert is_tool_allowed("export_paper", "plan") is False
    assert is_tool_allowed("export_paper", "qa") is False


@pytest.mark.parametrize(
    ("output_format", "extension", "expected_argument"),
    [("word", "docx", "--to=docx"), ("pdf", "pdf", "--pdf-engine=/fake/tectonic")],
)
def test_export_word_and_pdf_use_fixed_outputs(
    tmp_path,
    output_format,
    extension,
    expected_argument,
):
    _project_with_paper(tmp_path, "![Figure](figures/result.png)\n")
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        output_arg = next(item for item in command if item.startswith("--output="))
        output = output_arg.split("=", 1)[1]
        with open(output, "wb") as stream:
            stream.write(b"exported")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = export_paper(
        tmp_path,
        output_format,
        pandoc_path="/fake/pandoc",
        tectonic_path="/fake/tectonic",
        runner=runner,
    )

    assert result["output"] == f"paper/main.{extension}"
    assert expected_argument in captured["command"]
    assert f"--resource-path={tmp_path / 'paper'}" in captured["command"]


@pytest.mark.asyncio
async def test_paper_latex_slash_command_reports_fixed_output(tmp_path, monkeypatch):
    app = AeroApp.__new__(AeroApp)
    app._project_dir = tmp_path
    app._set_footer_status = lambda _message: None
    shown = []
    app._show_checkpoint_message = lambda message, **kwargs: shown.append((message, kwargs))
    monkeypatch.setattr(
        "aero.cli.main.export_paper",
        lambda project_dir, output_format, env: {
            "source": "paper/main.md",
            "output": f"paper/main.{'tex' if output_format == 'latex' else output_format}",
            "size": 100,
        },
    )

    await app._handle_paper_command("/paper export latex")

    assert "LaTeX 论文已生成" in shown[0][0]
    assert "paper/main.tex" in shown[0][0]
    assert shown[0][1] == {"force_scroll": True}


@pytest.mark.asyncio
async def test_pandoc_install_updates_footer_status(tmp_path, monkeypatch):
    app = AeroApp(AeroConfig(), persist_config=False)
    app._project_dir = tmp_path
    footer = []

    async def confirm(_screen):
        return "allow"

    exports = iter(
        [
            PaperExportError("缺少 Pandoc，无法导出论文。", missing_tools=["pandoc"]),
            {
                "source": "paper/main.md",
                "output": "paper/main.tex",
                "size": 100,
            },
        ]
    )

    def fake_export(*args, **kwargs):
        result = next(exports)
        if isinstance(result, Exception):
            raise result
        return result

    async def fake_install(tools):
        assert tools == ["pandoc"]
        return {"status": "success"}

    monkeypatch.setattr("aero.cli.main.export_paper", fake_export)
    monkeypatch.setattr("aero.agent.runtime.Runtime._build_exec_env", lambda: {})
    monkeypatch.setattr("aero.toolbox.tools.runtime.ensure_runtime_tools", fake_install)

    async with app.run_test(size=(100, 30)):
        app._show_checkpoint_message = lambda *args, **kwargs: None
        app._set_footer_status = footer.append
        app.push_screen_wait = confirm
        await app._handle_paper_command("/paper export latex")

    assert footer == [
        "正在安装 Pandoc…",
        "转换组件安装完成，正在导出论文…",
        "LaTeX 论文已生成",
    ]
