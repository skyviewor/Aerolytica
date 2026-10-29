"""Tests for Aero's private Micromamba runtime."""

import io
import os
import subprocess
import tarfile
from pathlib import Path

from aero.cli import init_runtime
from aero.core import runtime_paths


def completed(command: list[str], returncode: int = 0, stdout: str = ""):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")


def configure_runtime(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "aero-home" / "runtime"
    micromamba = root / "bin" / "micromamba"
    env = root / "envs" / "aero-agent"
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))
    return root, micromamba, env


def test_setup_runtime_interactively_persists_custom_root(monkeypatch, tmp_path):
    home = tmp_path / "aero-home"
    custom = tmp_path / "large-disk" / "aero-runtime"
    monkeypatch.setenv("AERO_HOME", str(home))
    monkeypatch.delenv("AERO_RUNTIME_ROOT", raising=False)
    monkeypatch.setattr(init_runtime.sys.stdin, "isatty", lambda: True)
    answers = iter([str(custom), ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(init_runtime, "ensure_micromamba", lambda: None)

    assert init_runtime.setup_runtime() is False
    assert (home / runtime_paths.RUNTIME_ROOT_CONFIG).read_text().strip() == str(custom)
    assert runtime_paths.runtime_root() == custom


def test_setup_runtime_interactive_empty_path_keeps_default(monkeypatch, tmp_path):
    home = tmp_path / "aero-home"
    monkeypatch.setenv("AERO_HOME", str(home))
    monkeypatch.delenv("AERO_RUNTIME_ROOT", raising=False)
    monkeypatch.setattr(init_runtime.sys.stdin, "isatty", lambda: True)
    answers = iter(["", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(init_runtime, "ensure_micromamba", lambda: None)

    assert init_runtime.setup_runtime() is False
    expected = (home / "runtime").resolve()
    assert runtime_paths.runtime_root() == expected
    assert (home / runtime_paths.RUNTIME_ROOT_CONFIG).read_text().strip() == str(expected)


def test_setup_runtime_creates_private_python_312_environment(monkeypatch, tmp_path):
    root, micromamba, env = configure_runtime(monkeypatch, tmp_path)
    micromamba.parent.mkdir(parents=True)
    micromamba.write_text("")
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int):
        calls.append(command)
        (env / "bin").mkdir(parents=True, exist_ok=True)
        (env / "bin" / "python").write_text("")
        return completed(command)

    monkeypatch.setattr(init_runtime, "_run", fake_run)

    assert init_runtime.setup_runtime() is True
    assert calls[0][:3] == [str(micromamba), "create", "--yes"]
    assert ["--root-prefix", str(root)] == calls[0][3:5]
    assert ["--prefix", str(env)] == calls[0][5:7]
    assert "python=3.12" in calls[0]


def test_setup_runtime_china_mirror_forces_mainland_package_sources(monkeypatch, tmp_path, capsys):
    root, micromamba, env = configure_runtime(monkeypatch, tmp_path)
    micromamba.parent.mkdir(parents=True)
    micromamba.write_text("")
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_text("")

    assert init_runtime.setup_runtime(china_mirror=True, assume_yes=True) is True

    assert "大陆镜像" in capsys.readouterr().out
    assert os.environ.get("AERO_NETWORK_REGION") is None


def test_setup_runtime_full_installs_conda_then_pip_packages(monkeypatch, tmp_path):
    root, micromamba, env = configure_runtime(monkeypatch, tmp_path)
    micromamba.parent.mkdir(parents=True)
    micromamba.write_text("")
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_text("")
    (env / "bin" / "mplfonts").write_text("")
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int):
        calls.append(command)
        return completed(command)

    monkeypatch.setattr(init_runtime, "_run", fake_run)

    assert init_runtime.setup_runtime(full=True) is True
    assert calls[0][:3] == [str(micromamba), "env", "update"]
    assert str(init_runtime.CONDA_ENVIRONMENT_FILE) in calls[0]
    assert calls[1][:4] == [str(env / "bin" / "python"), "-m", "pip", "install"]
    assert str(init_runtime.PIP_REQUIREMENTS_FILE) in calls[1]
    assert calls[2][-1] == "init"
    assert calls[3][-1] == "updaterc"


def test_ensure_micromamba_extracts_managed_binary(monkeypatch, tmp_path):
    _root, micromamba, _env = configure_runtime(monkeypatch, tmp_path)

    def fake_download(_url: str, destination: Path, **_kwargs):
        payload = b"#!/bin/sh\n"
        with tarfile.open(destination, "w:bz2") as archive:
            info = tarfile.TarInfo("bin/micromamba")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    monkeypatch.setattr(init_runtime, "download", fake_download)

    assert init_runtime.ensure_micromamba() == micromamba
    assert micromamba.read_bytes() == b"#!/bin/sh\n"
    assert micromamba.stat().st_mode & 0o111


def test_installer_subprocess_streams_output_to_terminal(monkeypatch, tmp_path):
    root, _micromamba, _env = configure_runtime(monkeypatch, tmp_path)
    captured = {}

    def fake_subprocess_run(command, **kwargs):
        captured.update(kwargs)
        return completed(command)

    monkeypatch.setattr(init_runtime.subprocess, "run", fake_subprocess_run)

    init_runtime._run(["installer", "--verbose"], timeout=30)

    assert captured["timeout"] == 30
    assert captured["check"] is False
    assert captured["env"]["MAMBA_ROOT_PREFIX"] == str(root)
    assert "capture_output" not in captured
    assert "stdout" not in captured
    assert "stderr" not in captured


def test_clean_runtime_only_removes_private_runtime(monkeypatch, tmp_path):
    root, _micromamba, _env = configure_runtime(monkeypatch, tmp_path)
    project = tmp_path / "project"
    (root / "envs").mkdir(parents=True)
    project.mkdir()
    (project / "aero.yaml").write_text("output: {}\n")

    assert init_runtime.clean_runtime(assume_yes=True) is True
    assert not root.exists()
    assert (project / "aero.yaml").exists()


def test_runtime_diagnostics_requires_python_312(monkeypatch, tmp_path):
    _root, micromamba, env = configure_runtime(monkeypatch, tmp_path)
    micromamba.parent.mkdir(parents=True)
    micromamba.write_text("")
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_text("")
    monkeypatch.setattr(
        init_runtime,
        "_run",
        lambda command, timeout, capture=False: completed(command, stdout="3.12.9\n"),
    )

    healthy, checks = init_runtime.runtime_diagnostics()

    assert healthy is True
    assert checks[-1] == ("Python 版本", True, "3.12.9")


def test_common_package_files_keep_pip_only_runtime_packages():
    assert "python=3.12" in init_runtime._conda_packages()
    assert "h5netcdf" in init_runtime._conda_packages()
    assert "cnmaps" not in init_runtime._conda_packages()
    assert init_runtime._pip_packages() == (
        "mplfonts",
        "cnmaps",
        "cyeva>=0.2.3,<0.3",
        "pint==0.24.3",
        "flexparser>=0.3.1,<0.4",
    )
    requirements = init_runtime.PIP_REQUIREMENTS_FILE.read_text()
    assert "pint==0.24.3" in requirements
    assert "flexparser>=0.3.1,<0.4" in requirements


def test_common_packages_retry_official_pypi_when_auto_mirror_misses_package(monkeypatch, tmp_path):
    _root, micromamba, _env = configure_runtime(monkeypatch, tmp_path)
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.setattr(init_runtime, "initialize_mplfonts", lambda _env: True)
    calls = []

    def fake_run(command, *, timeout):
        calls.append(command)
        return completed(command, returncode=1 if len(calls) == 2 else 0)

    monkeypatch.setattr(init_runtime, "_run", fake_run)
    assert init_runtime.install_common_packages(micromamba)
    assert len(calls) == 3
    assert calls[2][-2:] == ["--index-url", "https://pypi.org/simple"]


def test_conda_helper_documents_managed_runtime_and_pip_only_cnmaps():
    skill_text = (
        Path(__file__).resolve().parents[1] / "src/aero/skills/builtin/conda-helper/SKILL.md"
    ).read_text()
    assert "Micromamba and Python paths are platform-specific" in skill_text
    assert "aero-agent" in skill_text
    assert "ensure_runtime_tools" in skill_text
    assert "`cnmaps` is always pip-only" in skill_text
    assert "Never run `conda create`, `conda install`, or `conda activate`" in skill_text
    assert "conda create -n aero-agent" not in skill_text
