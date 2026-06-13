"""Tests for interactive runtime setup during ``aero init``."""

import subprocess
from pathlib import Path

from aero.cli import init_runtime


def completed(command: list[str], returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout="", stderr="")


def test_setup_runtime_uses_existing_conda_and_installs_common_packages(
    monkeypatch, tmp_path, capsys
):
    root = tmp_path / "miniconda3"
    conda = root / "bin" / "conda"
    env_bin = root / "envs" / "aero-agent" / "bin"
    conda.parent.mkdir(parents=True)
    conda.write_text("")
    answers = iter(("y",))
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int):
        calls.append(command)
        if command[:3] == [str(conda), "create", "-n"]:
            env_bin.mkdir(parents=True)
        elif command[:3] == [str(conda), "install", "-n"]:
            (env_bin / "mamba").write_text("")
        return completed(command)

    monkeypatch.setattr(init_runtime, "find_conda", lambda: conda)
    monkeypatch.setattr(init_runtime, "_run", fake_run)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert init_runtime.setup_runtime() is True
    assert calls[0][:4] == [str(conda), "create", "-n", "aero-agent"]
    assert "python=3.12" in calls[0]
    assert calls[1][:4] == [str(conda), "install", "-n", "aero-agent"]
    assert Path(calls[2][0]).resolve() == (env_bin / "mamba").resolve()
    assert calls[2][1:3] == ["env", "update"]
    assert str(init_runtime.CONDA_ENVIRONMENT_FILE) in calls[2]
    assert Path(calls[3][0]).resolve() == (env_bin / "python").resolve()
    assert calls[3][1:4] == ["-m", "pip", "install"]
    assert str(init_runtime.PIP_REQUIREMENTS_FILE) in calls[3]
    assert Path(calls[4][0]).resolve() == (env_bin / "mplfonts").resolve()
    assert calls[4][1:] == ["init"]
    assert Path(calls[5][0]).resolve() == (env_bin / "mplfonts").resolve()
    assert calls[5][1:] == ["updaterc"]
    output = capsys.readouterr().out
    assert "科学计算常用包" in output
    assert "environment.yaml" in output
    assert "env-requirements.txt" in output
    assert "libnetcdf" in output
    assert "mplfonts" in output
    assert "cnmaps" in output
    assert "Matplotlib 中文字体已初始化" in output


def test_install_common_packages_fails_when_mplfonts_init_fails(monkeypatch, tmp_path):
    root = tmp_path / "miniconda3"
    conda = root / "bin" / "conda"
    env_path = root / "envs" / "aero-agent"
    (env_path / "bin").mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int):
        calls.append(command)
        return completed(command, returncode=1 if command[-1:] == ["init"] else 0)

    monkeypatch.setattr(init_runtime, "_run", fake_run)

    assert init_runtime.install_common_packages(conda) is False
    assert Path(calls[-1][0]).resolve() == (env_path / "bin" / "mplfonts").resolve()
    assert calls[-1][1:] == ["init"]


def test_initialize_mplfonts_persists_configuration_after_init(monkeypatch, tmp_path):
    env_path = tmp_path / "aero-agent"
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int):
        calls.append(command)
        return completed(command)

    monkeypatch.setattr(init_runtime, "_run", fake_run)

    assert init_runtime.initialize_mplfonts(env_path) is True
    assert [command[1] for command in calls] == ["init", "updaterc"]


def test_common_package_files_separate_conda_and_pip_packages():
    conda_packages = init_runtime._conda_packages()
    pip_packages = init_runtime._pip_packages()

    assert "libnetcdf" in conda_packages
    assert "cartopy" in conda_packages
    assert "mplfonts" not in conda_packages
    assert "cnmaps" not in conda_packages
    assert pip_packages == ("mplfonts", "cnmaps")


def test_conda_helper_documents_python_312_and_pip_only_cnmaps():
    skill_text = (
        Path("src/aero/skills/builtin/conda-helper/SKILL.md").read_text()
    )

    assert "python=3.12" in skill_text
    assert "`cnmaps` is pip-only" in skill_text
    assert "Never install `cnmaps` with conda or mamba" in skill_text
    assert "python -m pip install -U cnmaps" in skill_text


def test_setup_runtime_can_skip_miniconda_install(monkeypatch):
    monkeypatch.setattr(init_runtime, "find_conda", lambda: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    assert init_runtime.setup_runtime() is False


def test_install_miniconda_uses_selected_path(monkeypatch, tmp_path):
    install_path = tmp_path / "conda"

    def fake_urlretrieve(_url: str, destination):
        destination.write_text("#!/bin/bash\n")

    def fake_run(command: list[str], *, timeout: int):
        conda = install_path / "bin" / "conda"
        conda.parent.mkdir(parents=True)
        conda.write_text("")
        return completed(command)

    monkeypatch.setattr(init_runtime.urllib.request, "urlretrieve", fake_urlretrieve)
    monkeypatch.setattr(init_runtime, "_run", fake_run)
    monkeypatch.setattr(
        init_runtime,
        "_miniconda_installer_url",
        lambda: "https://example.test/miniconda.sh",
    )

    assert init_runtime.install_miniconda(install_path) == install_path / "bin" / "conda"
