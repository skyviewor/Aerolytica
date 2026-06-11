from pathlib import Path


def test_runtime_exec_env_prepends_meteora_agent_bin(monkeypatch, tmp_path):
    from meteora.agent.runtime import Runtime

    root = tmp_path / "miniconda3"
    conda_bin = root / "bin"
    tool_bin = root / "envs" / "meteora-agent" / "bin"
    conda_bin.mkdir(parents=True)
    tool_bin.mkdir(parents=True)
    conda = conda_bin / "conda"
    conda.write_text("#!/bin/sh\n")

    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    monkeypatch.delenv("MAMBA_PREFIX", raising=False)
    monkeypatch.delenv("MAMBA_EXE", raising=False)
    monkeypatch.setenv("CONDA_EXE", str(conda))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    env = Runtime._build_exec_env()

    path_parts = env["PATH"].split(":")
    assert path_parts[:2] == [str(tool_bin), str(conda_bin)]
    assert "/usr/bin" in path_parts


def test_run_subprocess_uses_pipefail():
    from meteora.agent.runtime import Runtime

    result = Runtime().run_subprocess("false | true")

    assert result.success is False
    assert result.exit_code != 0
