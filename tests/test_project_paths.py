"""Tests for project-root path handling."""

import pytest


def test_find_project_dir_uses_pyproject_from_nested_lab(monkeypatch, tmp_path):
    from meteora.toolbox.paths import find_project_dir

    project = tmp_path / "meteora"
    nested = project / "lab"
    (project / "src" / "meteora").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'meteora'\n")
    nested.mkdir()

    monkeypatch.chdir(nested)

    assert find_project_dir() == project


@pytest.mark.asyncio
async def test_write_file_resolves_relative_path_from_project_root(monkeypatch, tmp_path):
    from meteora.toolbox.file_access import READ_FILES
    from meteora.toolbox.tools.files import write_file

    project = tmp_path / "meteora"
    nested = project / "lab"
    (project / "src" / "meteora").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'meteora'\n")
    nested.mkdir()
    monkeypatch.chdir(nested)
    READ_FILES.clear()

    result = await write_file("scripts/tmp/plot.py", "print('ok')\n")

    assert result["status"] == "success"
    assert (project / "scripts" / "tmp" / "plot.py").exists()
    assert not (nested / "scripts" / "tmp" / "plot.py").exists()


def test_dataset_default_output_dir_uses_meteora_project_data(monkeypatch, tmp_path):
    from meteora.toolbox.tools.datasets import _default_dataset_output_dir

    project = tmp_path / "meteora" / "lab"
    project.mkdir(parents=True)
    (project / "meteora.yaml").write_text("language: zh\n")
    monkeypatch.chdir(project)

    assert _default_dataset_output_dir() == project / "data"


def test_init_workspace_dirs_creates_structure(tmp_path):
    from meteora.cli.main import _create_workspace_dirs

    _create_workspace_dirs(tmp_path)

    for relative_path in ("data", "figures", "scripts/tmp", "plans", "literature"):
        assert (tmp_path / relative_path).is_dir()


def test_init_operates_on_current_directory(monkeypatch, tmp_path):
    from meteora.cli.main import _init

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("meteora.cli.init_runtime.setup_runtime", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("unexpected init prompt"))

    _init()

    assert (tmp_path / "meteora.yaml").is_file()
    assert not (tmp_path / "meteora-project").exists()
    for relative_path in ("data", "figures", "scripts/tmp", "plans", "literature"):
        assert (tmp_path / relative_path).is_dir()
