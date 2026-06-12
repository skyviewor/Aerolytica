"""Tests for Aero toolbox registry."""

import shlex
import subprocess
from pathlib import Path

import pytest

from aero.toolbox.registry import ToolRegistry, get_registry


def test_register_tool_decorator():
    from aero.core.types import ToolSpec
    from aero.toolbox.registry import ToolRegistry

    reg = ToolRegistry()

    def test_tool_fn(x: int) -> int:
        return x * 2

    spec = ToolSpec(
        name="test_tool",
        description="A test tool",
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "input x"},
            },
            "required": ["x"],
        },
        function=test_tool_fn,
    )
    reg.register(spec)
    assert reg.get("test_tool") is spec
    assert reg.get("nonexistent") is None


def test_tool_registry():
    reg = ToolRegistry()

    from aero.core.types import ToolSpec

    spec = ToolSpec(
        name="calc",
        description="Calculate something",
        parameters={"type": "object", "properties": {}},
        function=lambda: 42,
    )
    reg.register(spec)
    assert reg.get("calc") is spec
    assert len(reg.list_all()) == 1


def test_tool_to_llm_function():
    from aero.core.types import ToolSpec

    spec = ToolSpec(
        name="calc",
        description="Calculate something",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
        },
        function=lambda x: x,
    )
    fn_def = spec.to_llm_function()
    assert fn_def["type"] == "function"
    assert fn_def["function"]["name"] == "calc"
    assert fn_def["function"]["description"] == "Calculate something"


def test_registry_list_functions():
    from aero.core.types import ToolSpec

    reg = ToolRegistry()
    spec = ToolSpec(
        name="calc",
        description="Calculate",
        parameters={"type": "object", "properties": {}},
        function=lambda: 42,
    )
    reg.register(spec)
    funcs = reg.list_functions()
    assert len(funcs) == 1
    assert funcs[0]["function"]["name"] == "calc"


def test_tool_requires_confirmation():
    from aero.core.types import ToolSpec

    spec = ToolSpec(
        name="dangerous",
        description="Dangerous op",
        parameters={"type": "object", "properties": {}},
        function=lambda: None,
        requires_confirmation=True,
    )
    assert spec.requires_confirmation is True
    fn_def = spec.to_llm_function()
    assert "requires_confirmation" not in fn_def
    assert "requires_confirmation" not in fn_def["function"]


def test_register_tool_with_confirmation():
    from aero.core.types import ToolSpec
    from aero.toolbox.registry import ToolRegistry

    reg = ToolRegistry()
    spec = ToolSpec(
        name="delete_something",
        description="Delete something",
        parameters={"type": "object", "properties": {}},
        function=lambda: None,
        requires_confirmation=True,
    )
    reg.register(spec)
    retrieved = reg.get("delete_something")
    assert retrieved is not None
    assert retrieved.requires_confirmation is True


def test_run_shell_description_prefers_cli_data_tools():
    from aero.toolbox import builtin_tools  # noqa: F401

    spec = get_registry().get("run_shell")

    assert spec is not None
    assert "下载远程文件" in spec.description
    assert "不要跳过下载工具和 CLI 直接写 Python HTTP/Range/下载脚本" in spec.description
    assert "curl -L -C -" in spec.description
    assert "wget -c" in spec.description
    assert "aria2c" in spec.description
    assert "GRIB/GRIB2/NetCDF" in spec.description
    assert "才用 Python/cfgrib/xarray 脚本兜底" in spec.description
    assert "cdo -f nc copy" in spec.description
    assert "ncrcat" in spec.description
    assert "grib_to_netcdf" in spec.description
    assert "ensure_runtime_tools" in spec.description
    assert "通常不需要手动 conda activate" in spec.description
    assert "执行 python/python3/pip/pip3/python -m pip，都必须解析到 aero-agent" in spec.description
    assert "aero-agent" in spec.description
    assert "run_shell 会拒绝使用未纳入 aero-agent 的受管数据工具" in spec.description
    assert "先安装并尝试 CLI，再按需用脚本兜底" in spec.description


@pytest.mark.asyncio
async def test_ensure_runtime_tools_skips_install_when_ready(monkeypatch, tmp_path):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    root = tmp_path / "miniconda3"
    base_bin = root / "bin"
    env_bin = root / "envs" / "aero-agent" / "bin"
    base_bin.mkdir(parents=True)
    env_bin.mkdir(parents=True)
    conda = base_bin / "conda"
    mamba = env_bin / "mamba"
    conda.write_text("#!/bin/sh\n")
    mamba.write_text("#!/bin/sh\n")
    for tool in ("cdo", "grib_to_netcdf", "grib_copy", "grib_filter", "grib_ls", "grib_dump"):
        path = env_bin / tool
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)

    def fake_run(cmd, **kwargs):
        raise AssertionError("should not install when requested tools are already ready")

    monkeypatch.setattr(
        Runtime,
        "_build_exec_env",
        staticmethod(lambda: {"PATH": f"{env_bin}:{base_bin}", "CONDA_EXE": str(conda)}),
    )
    from aero.toolbox.runtime_manager import get_runtime_tool_manager

    manager = get_runtime_tool_manager()
    monkeypatch.setattr(manager, "find_conda_executable", lambda env: str(conda))
    monkeypatch.setattr(manager, "conda_env_exists", lambda conda_path, env: True)
    monkeypatch.setattr(manager, "command_runner", fake_run)

    result = await builtin_tools.ensure_runtime_tools(["cdo", "grib_to_netcdf"])

    assert result["status"] == "success"
    assert result["already_ready"] is True
    assert {item["tool"] for item in result["verified"]} == {"cdo", "grib_to_netcdf"}


@pytest.mark.asyncio
async def test_ensure_runtime_tools_installs_missing_and_symlinks(monkeypatch, tmp_path):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    root = tmp_path / "miniconda3"
    base_bin = root / "bin"
    env_bin = root / "envs" / "aero-agent" / "bin"
    base_bin.mkdir(parents=True)
    env_bin.mkdir(parents=True)
    conda = base_bin / "conda"
    mamba = env_bin / "mamba"
    conda.write_text("#!/bin/sh\n")
    mamba.write_text("#!/bin/sh\n")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        for tool in ("cdo", "grib_to_netcdf", "grib_copy", "grib_filter", "grib_ls", "grib_dump"):
            path = env_bin / tool
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(
        Runtime,
        "_build_exec_env",
        staticmethod(lambda: {"PATH": f"{env_bin}:{base_bin}", "CONDA_EXE": str(conda)}),
    )
    from aero.toolbox.runtime_manager import get_runtime_tool_manager

    manager = get_runtime_tool_manager()
    monkeypatch.setattr(manager, "find_conda_executable", lambda env: str(conda))
    monkeypatch.setattr(manager, "conda_env_exists", lambda conda_path, env: True)
    monkeypatch.setattr(manager, "command_runner", fake_run)

    result = await builtin_tools.ensure_runtime_tools(["cdo", "grib_to_netcdf"])

    assert result["status"] == "success"
    assert result["packages"] == ["cdo", "eccodes"]
    assert calls[0][:4] == [str(mamba), "install", "-p", str(env_bin.parent)]
    assert result["package_manager"] == str(mamba)
    assert (base_bin / "cdo").is_symlink()
    assert (base_bin / "grib_to_netcdf").is_symlink()


@pytest.mark.asyncio
async def test_ensure_runtime_tools_installs_mamba_when_missing(monkeypatch, tmp_path):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    root = tmp_path / "miniconda3"
    base_bin = root / "bin"
    env_bin = root / "envs" / "aero-agent" / "bin"
    base_bin.mkdir(parents=True)
    env_bin.mkdir(parents=True)
    conda = base_bin / "conda"
    mamba = env_bin / "mamba"
    conda.write_text("#!/bin/sh\n")
    for tool in ("cdo",):
        path = env_bin / tool
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:5] == [str(conda), "install", "-n", "aero-agent", "-c"]:
            mamba.write_text("#!/bin/sh\n")
            mamba.chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(
        Runtime, "_build_exec_env", staticmethod(lambda: {"PATH": f"{env_bin}:{base_bin}"})
    )
    from aero.toolbox.runtime_manager import get_runtime_tool_manager

    manager = get_runtime_tool_manager()
    monkeypatch.setattr(manager, "find_conda_executable", lambda env: str(conda))
    monkeypatch.setattr(manager, "conda_env_exists", lambda conda_path, env: False)
    monkeypatch.setattr(manager, "command_runner", fake_run)

    result = await builtin_tools.ensure_runtime_tools(["cdo"])

    assert result["status"] == "success"
    assert calls[0] == [
        str(conda),
        "create",
        "-n",
        "aero-agent",
        "-c",
        "conda-forge",
        "--override-channels",
        "python",
        "-y",
    ]
    assert calls[1] == [
        str(conda),
        "install",
        "-n",
        "aero-agent",
        "-c",
        "conda-forge",
        "--override-channels",
        "mamba",
        "-y",
    ]
    assert calls[2][:4] == [str(mamba), "install", "-p", str(env_bin.parent)]
    assert result["env_create_command"] == " ".join(calls[0])
    assert result["mamba_install_command"] == " ".join(calls[1])
    assert result["package_manager"] == str(mamba)


@pytest.mark.asyncio
async def test_ensure_runtime_tools_falls_back_to_conda_when_mamba_install_fails(
    monkeypatch, tmp_path
):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    root = tmp_path / "miniconda3"
    base_bin = root / "bin"
    env_bin = root / "envs" / "aero-agent" / "bin"
    base_bin.mkdir(parents=True)
    env_bin.mkdir(parents=True)
    conda = base_bin / "conda"
    conda.write_text("#!/bin/sh\n")
    cdo = env_bin / "cdo"
    cdo.write_text("#!/bin/sh\n")
    cdo.chmod(0o755)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == [
            str(conda),
            "install",
            "-n",
            "aero-agent",
            "-c",
            "conda-forge",
            "--override-channels",
            "mamba",
            "-y",
        ]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="solve failed")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(
        Runtime, "_build_exec_env", staticmethod(lambda: {"PATH": f"{env_bin}:{base_bin}"})
    )
    from aero.toolbox.runtime_manager import get_runtime_tool_manager

    manager = get_runtime_tool_manager()
    monkeypatch.setattr(manager, "find_conda_executable", lambda env: str(conda))
    monkeypatch.setattr(manager, "conda_env_exists", lambda conda_path, env: False)
    monkeypatch.setattr(manager, "command_runner", fake_run)

    result = await builtin_tools.ensure_runtime_tools(["cdo"])

    assert result["status"] == "success"
    assert calls[0] == [
        str(conda),
        "create",
        "-n",
        "aero-agent",
        "-c",
        "conda-forge",
        "--override-channels",
        "python",
        "-y",
    ]
    assert calls[1] == [
        str(conda),
        "install",
        "-n",
        "aero-agent",
        "-c",
        "conda-forge",
        "--override-channels",
        "mamba",
        "-y",
    ]
    assert calls[2] == [
        str(conda),
        "install",
        "-n",
        "aero-agent",
        "-c",
        "conda-forge",
        "--override-channels",
        "cdo",
        "-y",
    ]
    assert result["package_manager"] == str(conda)
    assert result["mamba_install_error"] == "mamba 安装失败。"


@pytest.mark.asyncio
async def test_run_shell_rejects_managed_tool_outside_aero_agent(monkeypatch, tmp_path):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    base_bin = tmp_path / "miniconda3" / "bin"
    base_bin.mkdir(parents=True)
    grib_to_netcdf = base_bin / "grib_to_netcdf"
    grib_to_netcdf.write_text("#!/bin/sh\n")
    grib_to_netcdf.chmod(0o755)

    monkeypatch.setattr(Runtime, "_build_exec_env", staticmethod(lambda: {"PATH": str(base_bin)}))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    result = await builtin_tools.run_shell(
        "grib_to_netcdf -o out.nc in.grib2",
        "convert grib",
    )

    assert result["status"] == "error"
    assert result["tool_missing"] is True
    assert result["missing_tools"] == ["grib_to_netcdf"]
    assert result["suggested_tool"] == "ensure_runtime_tools"


@pytest.mark.asyncio
async def test_run_shell_allows_managed_tool_inside_aero_agent(monkeypatch, tmp_path):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    root = tmp_path / "miniconda3"
    env_bin = root / "envs" / "aero-agent" / "bin"
    env_bin.mkdir(parents=True)
    grib_to_netcdf = env_bin / "grib_to_netcdf"
    grib_to_netcdf.write_text("#!/bin/sh\n")
    grib_to_netcdf.chmod(0o755)

    async def fake_run_subprocess_streaming(
        self,
        command,
        workdir=".",
        timeout_ms=120000,
        output_limit=20000,
    ):
        from aero.agent.runtime import ExecutionResult

        return ExecutionResult(
            True, stdout="ok", stderr="", stdout_bytes=2, exit_code=0, duration_ms=1
        )

    def fake_env():
        return {"PATH": str(env_bin), "CONDA_EXE": str(root / "bin" / "conda")}

    monkeypatch.setattr("aero.agent.runtime.Runtime._build_exec_env", staticmethod(fake_env))
    monkeypatch.setattr(Runtime, "run_subprocess_streaming", fake_run_subprocess_streaming)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    result = await builtin_tools.run_shell(
        "grib_to_netcdf -o out.nc in.grib2",
        "convert grib",
    )

    assert result["status"] == "success"
    assert result["stdout"] == "ok"


@pytest.mark.asyncio
async def test_run_shell_rejects_python_outside_aero_agent(monkeypatch, tmp_path):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    base_bin = tmp_path / "miniconda3" / "bin"
    base_bin.mkdir(parents=True)
    python = base_bin / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)

    monkeypatch.setattr(Runtime, "_build_exec_env", staticmethod(lambda: {"PATH": str(base_bin)}))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    result = await builtin_tools.run_shell("python plot.py", "plot figure")

    assert result["status"] == "error"
    assert result["python_runtime_invalid"] is True
    assert result["failures"][0]["reason"] == "not_in_aero_agent"


@pytest.mark.asyncio
async def test_run_shell_rejects_absolute_system_python(monkeypatch, tmp_path):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    system_bin = tmp_path / "usr" / "bin"
    system_bin.mkdir(parents=True)
    python = system_bin / "python3"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)

    monkeypatch.setattr(Runtime, "_build_exec_env", staticmethod(lambda: {"PATH": str(system_bin)}))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    result = await builtin_tools.run_shell(f"{python} plot.py", "plot figure")

    assert result["status"] == "error"
    assert result["python_runtime_invalid"] is True
    assert result["failures"][0]["path"] == str(python)


@pytest.mark.asyncio
async def test_run_shell_allows_python_inside_aero_agent(monkeypatch, tmp_path):
    from aero.agent.runtime import ExecutionResult, Runtime
    from aero.toolbox import builtin_tools

    root = tmp_path / "miniconda3"
    env_bin = root / "envs" / "aero-agent" / "bin"
    env_bin.mkdir(parents=True)
    python = env_bin / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)

    async def fake_run_subprocess_streaming(
        self,
        command,
        workdir=".",
        timeout_ms=120000,
        output_limit=20000,
    ):
        return ExecutionResult(
            True, stdout="ok", stderr="", stdout_bytes=2, exit_code=0, duration_ms=1
        )

    monkeypatch.setattr(
        Runtime,
        "_build_exec_env",
        staticmethod(lambda: {"PATH": str(env_bin), "CONDA_EXE": str(root / "bin" / "conda")}),
    )
    monkeypatch.setattr(Runtime, "run_subprocess_streaming", fake_run_subprocess_streaming)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    result = await builtin_tools.run_shell("python plot.py", "plot figure")

    assert result["status"] == "success"
    assert result["stdout"] == "ok"


@pytest.mark.asyncio
async def test_run_shell_truncates_install_output_more_aggressively(monkeypatch):
    from aero.agent.runtime import ExecutionResult, Runtime
    from aero.toolbox import builtin_tools

    async def fake_run_subprocess_streaming(
        self,
        command,
        workdir=".",
        timeout_ms=120000,
        output_limit=20000,
    ):
        stdout = "x" * 9000
        return ExecutionResult(
            True,
            stdout=stdout[-output_limit:],
            stderr="",
            stdout_bytes=len(stdout),
            exit_code=0,
            duration_ms=1,
        )

    monkeypatch.setattr(Runtime, "run_subprocess_streaming", fake_run_subprocess_streaming)

    result = await builtin_tools.run_shell(
        "pip install xarray matplotlib cartopy netcdf4",
        "install plotting deps",
    )

    assert result["status"] == "success"
    assert result["output_truncated"] is True
    assert result["stdout_bytes"] == 9000
    assert len(result["stdout"]) < 8500


@pytest.mark.asyncio
async def test_run_shell_keeps_larger_non_install_output(monkeypatch):
    from aero.agent.runtime import ExecutionResult, Runtime
    from aero.toolbox import builtin_tools

    async def fake_run_subprocess_streaming(
        self,
        command,
        workdir=".",
        timeout_ms=120000,
        output_limit=20000,
    ):
        stdout = "x" * 9000
        return ExecutionResult(
            True,
            stdout=stdout[-output_limit:],
            stderr="",
            stdout_bytes=len(stdout),
            exit_code=0,
            duration_ms=1,
        )

    monkeypatch.setattr(Runtime, "run_subprocess_streaming", fake_run_subprocess_streaming)

    result = await builtin_tools.run_shell("python plot.py", "plot figure")

    assert result["status"] == "success"
    assert result["output_truncated"] is False
    assert result["stdout_bytes"] == 9000
    assert len(result["stdout"]) == 9000


@pytest.mark.asyncio
async def test_run_shell_streams_stdout_to_progress():
    import asyncio

    from aero.agent.progress import ProgressReporter, use_progress_reporter
    from aero.toolbox import builtin_tools

    queue = asyncio.Queue()
    reporter = ProgressReporter(asyncio.get_running_loop(), queue)
    command = "printf 'one\\n'; sleep 0.1; printf 'two\\n'"

    with use_progress_reporter(reporter):
        result = await builtin_tools.run_shell(
            command,
            "stream stdout",
            timeout_ms=10000,
        )

    seen = []
    while not queue.empty():
        seen.append(await queue.get())

    assert result["status"] == "success"
    assert "one" in result["stdout"]
    assert "two" in result["stdout"]
    assert any("stdout: one" in item for item in seen)
    assert any("stdout: two" in item for item in seen)


@pytest.mark.asyncio
async def test_run_shell_suppresses_benign_eccodes_time_warning_progress():
    import asyncio

    from aero.agent.progress import ProgressReporter, use_progress_reporter
    from aero.toolbox import builtin_tools

    queue = asyncio.Queue()
    reporter = ProgressReporter(asyncio.get_running_loop(), queue)
    warning = (
        "ECCODES ERROR   :   Key dataTime (unpack_long): "
        "Truncating time: non-zero seconds(37) ignored"
    )
    command = f"printf '%s\\n' {shlex.quote(warning)} >&2"

    with use_progress_reporter(reporter):
        result = await builtin_tools.run_shell(
            command,
            "stream stderr warning",
            timeout_ms=10000,
        )

    seen = []
    while not queue.empty():
        seen.append(await queue.get())

    assert result["status"] == "success"
    assert warning in result["stderr"]
    assert not any("ECCODES ERROR" in item for item in seen)


def test_normalize_shell_context_removes_missing_leading_cd(monkeypatch, tmp_path):
    from aero.toolbox.tools.runtime import _normalize_shell_context

    project = tmp_path / "project"
    (project / "src" / "aero").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='test'\n")
    monkeypatch.chdir(project)

    command, workdir, correction = _normalize_shell_context(
        "cd /home/user && python scripts/tmp/plot.py",
        ".",
    )

    assert command == "python scripts/tmp/plot.py"
    assert Path(workdir).resolve() == project.resolve()
    assert "当前工作根目录" in correction


@pytest.mark.asyncio
async def test_run_shell_uses_project_root_for_missing_workdir(monkeypatch, tmp_path):
    from aero.agent.runtime import ExecutionResult, Runtime
    from aero.toolbox import builtin_tools

    project = tmp_path / "project"
    (project / "src" / "aero").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='test'\n")
    monkeypatch.chdir(project)
    captured = {}

    async def fake_run_subprocess_streaming(
        self,
        command,
        workdir=".",
        timeout_ms=120000,
        output_limit=20000,
    ):
        captured.update(command=command, workdir=workdir)
        return ExecutionResult(True, stdout="ok", stderr="", exit_code=0, duration_ms=1)

    monkeypatch.setattr(Runtime, "run_subprocess_streaming", fake_run_subprocess_streaming)

    result = await builtin_tools.run_shell(
        "cd /home/user && python scripts/tmp/plot.py",
        "plot figure",
        workdir="/another/missing/path",
    )

    assert result["status"] == "success"
    assert captured["command"] == "python scripts/tmp/plot.py"
    assert Path(captured["workdir"]).resolve() == project.resolve()
    assert result["context_correction"]
