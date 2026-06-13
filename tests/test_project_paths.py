"""Tests for project-root path handling."""

import pytest


def test_find_project_dir_uses_current_directory(monkeypatch, tmp_path):
    from aero.toolbox.paths import find_project_dir

    project = tmp_path / "aero"
    nested = project / "lab"
    (project / "src" / "aero").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'aero'\n")
    nested.mkdir()

    monkeypatch.chdir(nested)

    assert find_project_dir() == nested


@pytest.mark.asyncio
async def test_write_file_resolves_relative_path_from_current_directory(monkeypatch, tmp_path):
    from aero.toolbox.file_access import READ_FILES
    from aero.toolbox.tools.files import write_file

    project = tmp_path / "aero"
    nested = project / "lab"
    (project / "src" / "aero").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'aero'\n")
    nested.mkdir()
    monkeypatch.chdir(nested)
    READ_FILES.clear()

    result = await write_file("scripts/tmp/plot.py", "print('ok')\n")

    assert result["status"] == "success"
    assert (nested / "scripts" / "tmp" / "plot.py").exists()
    assert not (project / "scripts" / "tmp" / "plot.py").exists()


def test_dataset_default_output_dir_uses_aero_project_data(monkeypatch, tmp_path):
    from aero.toolbox.tools.datasets import _default_dataset_output_dir

    project = tmp_path / "aero" / "lab"
    project.mkdir(parents=True)
    (project / "aero.yaml").write_text("language: zh\n")
    monkeypatch.chdir(project)

    assert _default_dataset_output_dir() == project / "data"


def test_current_directory_config_is_not_inherited_from_parent(monkeypatch, tmp_path):
    from aero.adapters.cds_adapter import config_output_dir
    from aero.toolbox.config_access import find_config_path

    project = tmp_path / "aero"
    nested = project / "lab"
    nested.mkdir(parents=True)
    (project / "aero.yaml").write_text("output:\n  data_dir: parent_data\n")
    monkeypatch.chdir(nested)

    assert find_config_path() == nested / "aero.yaml"
    assert config_output_dir() == nested / "data"


def test_init_workspace_dirs_creates_structure(tmp_path):
    from aero.cli.main import _create_workspace_dirs

    _create_workspace_dirs(tmp_path)

    for relative_path in ("data", "figures", "scripts/tmp", "plans", "literature"):
        assert (tmp_path / relative_path).is_dir()


def test_init_operates_on_current_directory(monkeypatch, tmp_path):
    from aero.cli.main import _init

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("aero.cli.init_runtime.setup_runtime", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("unexpected init prompt"))

    _init()

    assert (tmp_path / "aero.yaml").is_file()
    assert not (tmp_path / "aero-project").exists()
    for relative_path in ("data", "figures", "scripts/tmp", "plans", "literature"):
        assert (tmp_path / relative_path).is_dir()


@pytest.mark.asyncio
async def test_list_files_resolves_data_from_current_directory(monkeypatch, tmp_path):
    from aero.toolbox.tools.listing import list_files

    project = tmp_path / "aero"
    nested = project / "lab"
    data = nested / "data"
    data.mkdir(parents=True)
    (project / "aero.yaml").write_text("output:\n  data_dir: data\n")
    (data / "sample.nc").write_bytes(b"CDF")
    monkeypatch.chdir(nested)

    result = await list_files("data", pattern="nc")

    assert result["file_count"] == 1
    assert result["directory"] == "data"
    assert result["files"][0]["path"] == "data/sample.nc"


@pytest.mark.asyncio
async def test_inspect_nc_resolves_download_path_from_current_directory(
    monkeypatch,
    tmp_path,
):
    import sys
    from types import SimpleNamespace

    from aero.toolbox.tools.netcdf import inspect_nc

    class FakeCoord:
        values = ["2025-06-01T00:00:00"]

        def __len__(self):
            return len(self.values)

    class FakeVar:
        dims = ("time",)
        shape = (1,)
        dtype = "float32"
        attrs = {"units": "DU", "long_name": "Total column ozone"}

    class FakeDataset:
        sizes = {"time": 1}
        coords = {"time": FakeCoord()}
        data_vars = {"total_column_ozone": FakeVar()}

        def __getitem__(self, name):
            return self.data_vars[name]

        def close(self):
            pass

    project = tmp_path / "aero"
    nested = project / "lab"
    data = nested / "data"
    data.mkdir(parents=True)
    (project / "aero.yaml").write_text("output:\n  data_dir: data\n")
    (data / "sample.nc").write_bytes(b"CDF")
    monkeypatch.chdir(nested)
    monkeypatch.setitem(
        sys.modules,
        "xarray",
        SimpleNamespace(open_dataset=lambda path: FakeDataset()),
    )

    result = await inspect_nc("data/sample.nc")

    assert result["status"] == "ok"
    assert result["file"] == "data/sample.nc"
    assert "total_column_ozone" in result["variables"]
