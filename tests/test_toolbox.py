"""Tests for Aero toolbox registry."""

import shlex
import subprocess
from pathlib import Path

import pytest

from aero.toolbox.registry import ToolRegistry, get_registry


@pytest.mark.parametrize(
    "name",
    [
        "_download_progress_reporter",
        "_find_config",
        "_find_config_path",
        "_find_project_dir",
        "_fmt_duration",
        "_fmt_size",
        "_short_path",
        "check_web_search_config",
        "_ensure_vision_client",
        "_reuse_existing_era5_file",
        "_summarize_dataset",
        "_inspect_grib2_with_cfgrib",
        "_load_gfs_parameter_lookup",
        "_parse_grib2_messages",
        "_parse_grib2_product_metadata",
        "scan_local_files",
    ],
)
def test_legacy_builtin_tool_exports_remain_available(name):
    from aero.toolbox import builtin_tools

    assert callable(getattr(builtin_tools, name))


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
    assert "不要用 curl/wget/head/grep 抓网页或 API 查参数" in spec.description
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

    root = tmp_path / "runtime"
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))
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
async def test_ensure_runtime_tools_installs_missing_in_private_runtime(monkeypatch, tmp_path):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    root = tmp_path / "runtime"
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))
    base_bin = root / "bin"
    env_bin = root / "envs" / "aero-agent" / "bin"
    base_bin.mkdir(parents=True)
    env_bin.mkdir(parents=True)
    conda = base_bin / "conda"
    conda.write_text("#!/bin/sh\n")

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
    assert calls[0][:3] == [str(conda), "install", "--yes"]
    assert ["--root-prefix", str(root)] == calls[0][3:5]
    assert ["--prefix", str(env_bin.parent)] == calls[0][5:7]
    assert result["package_manager"] == str(conda)
    assert result["symlinks"] == []


@pytest.mark.asyncio
async def test_ensure_runtime_tools_creates_private_environment_when_missing(monkeypatch, tmp_path):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    root = tmp_path / "runtime"
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))
    base_bin = root / "bin"
    env_bin = root / "envs" / "aero-agent" / "bin"
    base_bin.mkdir(parents=True)
    env_bin.mkdir(parents=True)
    conda = base_bin / "conda"
    conda.write_text("#!/bin/sh\n")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "create":
            env_bin.mkdir(parents=True, exist_ok=True)
        else:
            path = env_bin / "cdo"
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
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
    assert calls[0][:3] == [str(conda), "create", "--yes"]
    assert ["--root-prefix", str(root)] == calls[0][3:5]
    assert ["--prefix", str(env_bin.parent)] == calls[0][5:7]
    assert calls[1][:3] == [str(conda), "install", "--yes"]
    assert result["env_create_command"] == " ".join(calls[0])
    assert result["mamba_install_command"] is None
    assert result["package_manager"] == str(conda)


@pytest.mark.asyncio
async def test_ensure_runtime_tools_bootstraps_managed_micromamba_when_missing(
    monkeypatch, tmp_path
):
    from aero.agent.runtime import Runtime
    from aero.cli import init_runtime
    from aero.toolbox import builtin_tools
    from aero.toolbox.runtime_manager import get_runtime_tool_manager

    root = tmp_path / "runtime"
    env_bin = root / "envs" / "aero-agent" / "bin"
    managed_micromamba = root / "bin" / "micromamba"
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))
    monkeypatch.setattr(Runtime, "_build_exec_env", staticmethod(lambda: {"PATH": str(env_bin)}))

    manager = get_runtime_tool_manager()
    monkeypatch.setattr(manager, "find_conda_executable", lambda env: None)
    monkeypatch.setattr(manager, "conda_env_exists", lambda conda_path, env: False)

    def fake_bootstrap():
        managed_micromamba.parent.mkdir(parents=True)
        managed_micromamba.write_text("#!/bin/sh\n")
        return managed_micromamba

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "create":
            env_bin.mkdir(parents=True, exist_ok=True)
        else:
            tool = env_bin / "cdo"
            tool.write_text("#!/bin/sh\n")
            tool.chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(init_runtime, "ensure_micromamba", fake_bootstrap)
    monkeypatch.setattr(manager, "command_runner", fake_run)

    result = await builtin_tools.ensure_runtime_tools(["cdo"])

    assert result["status"] == "success"
    assert calls[0][0] == str(managed_micromamba)
    assert ["--root-prefix", str(root)] == calls[0][3:5]
    assert ["--prefix", str(env_bin.parent)] == calls[0][5:7]
    assert result["package_manager"] == str(managed_micromamba)


@pytest.mark.asyncio
async def test_ensure_runtime_tools_reports_private_package_install_failure(monkeypatch, tmp_path):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    root = tmp_path / "runtime"
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))
    base_bin = root / "bin"
    env_bin = root / "envs" / "aero-agent" / "bin"
    base_bin.mkdir(parents=True)
    env_bin.mkdir(parents=True)
    conda = base_bin / "conda"
    conda.write_text("#!/bin/sh\n")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "install":
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

    assert result["status"] == "error"
    assert result["message"] == "运行时工具安装失败。"
    assert calls[0][1] == "create"
    assert calls[1][1] == "install"


@pytest.mark.asyncio
async def test_run_shell_rejects_managed_tool_outside_aero_agent(monkeypatch, tmp_path):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    base_bin = tmp_path / "miniconda3" / "bin"
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(tmp_path / "runtime"))
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

    root = tmp_path / "runtime"
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))
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
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(tmp_path / "runtime"))
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
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(tmp_path / "runtime"))
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

    root = tmp_path / "runtime"
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))
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


def test_multi_panel_cartopy_plot_validation_requires_extend_and_grid_locations(tmp_path):
    from aero.toolbox.tools.runtime import _scientific_plot_script_error

    script = tmp_path / "plot_bad.py"
    script.write_text(
        "import cartopy.crs as ccrs\n"
        "import matplotlib.pyplot as plt\n"
        "fig, axes = plt.subplots(3, 3, subplot_kw={'projection': ccrs.PlateCarree()})\n"
        "for ax in axes.flat:\n"
        "    ax.contourf(lon, lat, field, levels=levels)\n"
        "    ax.gridlines(draw_labels=False)\n",
        encoding="utf-8",
    )

    result = _scientific_plot_script_error(f"python {script}", str(tmp_path))

    assert result is not None
    assert result["scientific_plot_validation_failed"] is True
    assert len(result["violations"]) == 5


def test_cartopy_plot_validation_rejects_tight_bbox(tmp_path):
    from aero.toolbox.tools.runtime import _scientific_plot_script_error

    script = tmp_path / "plot_tight_bbox.py"
    script.write_text(
        "import cartopy.crs as ccrs\n"
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots(subplot_kw={'projection': ccrs.PlateCarree()})\n"
        "ax.contourf(lon, lat, field, levels=levels, extend='both')\n"
        "fig.savefig('map.png', bbox_inches='tight')\n",
        encoding="utf-8",
    )

    result = _scientific_plot_script_error(f"python {script}", str(tmp_path))

    assert result is not None
    assert result["scientific_plot_validation_failed"] is True
    assert "bbox_inches='tight'" in result["violations"][0]


def test_plot_validation_rejects_layout_passed_to_savefig(tmp_path):
    from aero.toolbox.tools.runtime import _scientific_plot_script_error

    script = tmp_path / "plot.py"
    script.write_text(
        "import matplotlib.pyplot as plt\n"
        "fig = plt.figure()\n"
        "fig.savefig('map.png', layout='compressed')\n",
        encoding="utf-8",
    )

    result = _scientific_plot_script_error(f"python {script}", str(tmp_path))

    assert result is not None
    assert "不能传给 savefig" in result["violations"][0]


def test_plot_validation_rejects_raw_string_with_doubled_mathtext_slashes(tmp_path):
    from aero.toolbox.tools.runtime import _scientific_plot_script_error

    script = tmp_path / "plot_bad_unit.py"
    script.write_text(
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots()\n"
        r'ax.set_xlabel(r"PVU ($10^{-6}\\,\\mathrm{K\\,m^2}$)")' + "\n",
        encoding="utf-8",
    )

    result = _scientific_plot_script_error(f"python {script}", str(tmp_path))

    assert result is not None
    assert "双反斜杠" in result["violations"][0]


def test_plot_validation_rejects_unbalanced_mathtext_delimiters(tmp_path):
    from aero.toolbox.tools.runtime import _scientific_plot_script_error

    script = tmp_path / "plot_bad_unit.py"
    script.write_text(
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots()\n"
        r'ax.set_xlabel(r"PVU (10^{-6}\,\mathrm{K}$)")' + "\n",
        encoding="utf-8",
    )

    result = _scientific_plot_script_error(f"python {script}", str(tmp_path))

    assert result is not None
    assert "未配对" in result["violations"][0]


def test_plot_validation_accepts_correct_raw_mathtext_unit(tmp_path):
    from aero.toolbox.tools.runtime import _scientific_plot_script_error

    script = tmp_path / "plot_good_unit.py"
    script.write_text(
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots()\n"
        r'ax.set_xlabel(r"PVU ($10^{-6}\,\mathrm{K\,m^2\,kg^{-1}\,s^{-1}}$)")' + "\n",
        encoding="utf-8",
    )

    assert _scientific_plot_script_error(f"python {script}", str(tmp_path)) is None


def test_plot_validation_rejects_mathtext_commands_outside_math_mode(tmp_path):
    from aero.toolbox.tools.runtime import _scientific_plot_script_error

    script = tmp_path / "plot_bad_unit.py"
    script.write_text(
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots()\n"
        r'ax.set_xlabel(r"PVU (0^{-6}\,\mathrm{K\,m^2\,kg^{-1}\,s^{-1}})")' + "\n",
        encoding="utf-8",
    )

    result = _scientific_plot_script_error(f"python {script}", str(tmp_path))

    assert result is not None
    assert any("`$...$` 外" in item for item in result["violations"])
    assert any("`10^{-6}` 误写成了 `0^{-6}`" in item for item in result["violations"])
    assert any("PVU 单位格式不规范" in item for item in result["violations"])


def test_plot_validation_rejects_noncanonical_pvu_unit(tmp_path):
    from aero.toolbox.tools.runtime import _scientific_plot_script_error

    script = tmp_path / "plot_bad_pvu_unit.py"
    script.write_text(
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots()\n"
        r'ax.set_xlabel(r"PVU ($10^{-6}\,\mathrm{K\,m^2\,s^{-1}}$)")' + "\n",
        encoding="utf-8",
    )

    result = _scientific_plot_script_error(f"python {script}", str(tmp_path))

    assert result is not None
    assert any("PVU 单位格式不规范" in item for item in result["violations"])


def test_multi_panel_cartopy_plot_validation_rejects_mixed_layout_owners(tmp_path):
    from aero.toolbox.tools.runtime import _scientific_plot_script_error

    script = tmp_path / "plot.py"
    script.write_text(
        "import cartopy.crs as ccrs\n"
        "import matplotlib.pyplot as plt\n"
        "fig, axes = plt.subplots(1, 2, layout='compressed', "
        "subplot_kw={'projection': ccrs.PlateCarree()})\n"
        "for ax in axes.flat:\n"
        "    ax.contourf([[1]], extend='both')\n"
        "    ax.gridlines(xlocs=[0], ylocs=[0], linestyle='--')\n"
        "fig.subplots_adjust(top=0.8)\n",
        encoding="utf-8",
    )

    result = _scientific_plot_script_error(f"python {script}", str(tmp_path))

    assert result is not None
    assert "只能有一个布局所有者" in result["violations"][0]


def test_multi_panel_cartopy_plot_validation_rejects_suptitle_outside_canvas(
    tmp_path,
):
    from aero.toolbox.tools.runtime import _scientific_plot_script_error

    script = tmp_path / "plot.py"
    script.write_text(
        "import cartopy.crs as ccrs\n"
        "import matplotlib.pyplot as plt\n"
        "fig, axes = plt.subplots(1, 2, layout='compressed', "
        "subplot_kw={'projection': ccrs.PlateCarree()})\n"
        "for ax in axes.flat:\n"
        "    ax.contourf([[1]], extend='both')\n"
        "    ax.gridlines(xlocs=[0], ylocs=[0], linestyle='--')\n"
        "fig.suptitle('Title', y=1.02)\n"
        "ensure_suptitle_clearance(fig, axes)\n",
        encoding="utf-8",
    )

    result = _scientific_plot_script_error(f"python {script}", str(tmp_path))

    assert result is not None
    assert "总标题不得用 y>=1" in result["violations"][0]


def test_multi_panel_cartopy_plot_validation_accepts_complete_pattern(tmp_path):
    from aero.toolbox.tools.runtime import _scientific_plot_script_error

    script = tmp_path / "plot_good.py"
    script.write_text(
        "import cartopy.crs as ccrs\n"
        "from cartopy.mpl.ticker import LatitudeFormatter, LongitudeFormatter\n"
        "import matplotlib.pyplot as plt\n"
        "fig, axes = plt.subplots(3, 3, layout='compressed', "
        "subplot_kw={'projection': ccrs.PlateCarree()})\n"
        "for ax in axes.flat:\n"
        "    ax.contourf(lon, lat, field, levels=levels, extend='both')\n"
        "    ax.set_xticks(lon_ticks, crs=ccrs.PlateCarree())\n"
        "    ax.set_yticks(lat_ticks, crs=ccrs.PlateCarree())\n"
        "    ax.xaxis.set_major_formatter(LongitudeFormatter())\n"
        "    ax.yaxis.set_major_formatter(LatitudeFormatter())\n"
        "    ax.tick_params(labelleft=True, labelbottom=True)\n"
        "    ax.gridlines(xlocs=lon_ticks, ylocs=lat_ticks, "
        "draw_labels=False, linestyle='--')\n"
        "assert_artists_inside_canvas(fig)\n",
        encoding="utf-8",
    )

    assert _scientific_plot_script_error(f"python {script}", str(tmp_path)) is None


def test_multi_panel_platecarree_rejects_missing_outer_coordinate_labels(tmp_path):
    from aero.toolbox.tools.runtime import _scientific_plot_script_error

    script = tmp_path / "plot_no_latitude_labels.py"
    script.write_text(
        "import cartopy.crs as ccrs\n"
        "import matplotlib.pyplot as plt\n"
        "fig, axes = plt.subplots(1, 2, layout='compressed', "
        "subplot_kw={'projection': ccrs.PlateCarree()})\n"
        "for ax in axes.flat:\n"
        "    ax.contourf(lon, lat, field, levels=levels, extend='both')\n"
        "    ax.gridlines(xlocs=lon_ticks, ylocs=lat_ticks, "
        "draw_labels=True, linestyle='--')\n"
        "assert_artists_inside_canvas(fig)\n",
        encoding="utf-8",
    )

    result = _scientific_plot_script_error(f"python {script}", str(tmp_path))

    assert result is not None
    assert any("外侧经纬度标签" in item for item in result["violations"])


def test_multi_panel_cartopy_plot_validation_requires_canvas_bounds_guard(tmp_path):
    from aero.toolbox.tools.runtime import _scientific_plot_script_error

    script = tmp_path / "plot_no_bounds_guard.py"
    script.write_text(
        "import cartopy.crs as ccrs\n"
        "import matplotlib.pyplot as plt\n"
        "fig, axes = plt.subplots(1, 2, layout='compressed', "
        "subplot_kw={'projection': ccrs.PlateCarree()})\n"
        "for ax in axes.flat:\n"
        "    ax.contourf(lon, lat, field, levels=levels, extend='both')\n"
        "    ax.gridlines(xlocs=lon_ticks, ylocs=lat_ticks, linestyle='--')\n",
        encoding="utf-8",
    )

    result = _scientific_plot_script_error(f"python {script}", str(tmp_path))

    assert result is not None
    assert any("assert_artists_inside_canvas(fig)" in item for item in result["violations"])


@pytest.mark.asyncio
async def test_run_shell_truncates_install_output_more_aggressively(monkeypatch, tmp_path):
    from aero.agent.runtime import ExecutionResult, Runtime
    from aero.toolbox import builtin_tools

    root = tmp_path / "runtime"
    env_bin = root / "envs" / "aero-agent" / "bin"
    env_bin.mkdir(parents=True)
    python = env_bin / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    pip = env_bin / "pip"
    pip.write_text("#!/bin/sh\n")
    pip.chmod(0o755)
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))

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
async def test_run_shell_blocks_cnmaps_in_mamba_install(monkeypatch, tmp_path):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    root = tmp_path / "runtime"
    env_bin = root / "envs" / "aero-agent" / "bin"
    env_bin.mkdir(parents=True)
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))

    async def should_not_execute(*args, **kwargs):
        raise AssertionError("blocked cnmaps conda command must not execute")

    monkeypatch.setattr(Runtime, "run_subprocess_streaming", should_not_execute)

    result = await builtin_tools.run_shell(
        (
            f"{env_bin}/mamba install -p {env_bin.parent} -c conda-forge "
            "xarray cartopy cnmaps matplotlib -y"
        ),
        "install plotting dependencies",
    )

    assert result["status"] == "error"
    assert result["cnmaps_conda_install_blocked"] is True
    assert result["suggested_command"] == (f"{env_bin}/python -m pip install -U cnmaps")
    assert "从 conda/mamba 包列表中移除 cnmaps" in result["message"]


def test_cnmaps_conda_guard_allows_pip_and_unrelated_conda_installs():
    from aero.toolbox.tools.runtime import _cnmaps_conda_install_error

    assert _cnmaps_conda_install_error("python -m pip install -U cnmaps") is None
    assert _cnmaps_conda_install_error("mamba install cartopy matplotlib -y") is None
    assert _cnmaps_conda_install_error("conda install cnmaps-data -y") is not None
    assert _cnmaps_conda_install_error("micromamba create -n plot cnmaps") is not None


def test_shell_guard_blocks_user_conda_and_allows_scoped_managed_micromamba(monkeypatch, tmp_path):
    from aero.toolbox.tools.runtime import _user_conda_runtime_error

    root = tmp_path / "runtime"
    env = root / "envs" / "aero-agent"
    managed = root / "bin" / "micromamba"
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))

    assert _user_conda_runtime_error("conda create -n aero-agent python=3.12") is not None
    assert (
        _user_conda_runtime_error("/Users/test/miniconda3/envs/aero-agent/bin/mamba install nco -y")
        is not None
    )
    assert _user_conda_runtime_error("micromamba install nco -y") is not None
    assert (
        _user_conda_runtime_error(
            f"{managed} install --root-prefix {root} --prefix {env} nco --yes"
        )
        is None
    )


@pytest.mark.asyncio
async def test_run_shell_keeps_larger_non_install_output(monkeypatch, tmp_path):
    from aero.agent.runtime import ExecutionResult, Runtime
    from aero.toolbox import builtin_tools

    root = tmp_path / "runtime"
    env_bin = root / "envs" / "aero-agent" / "bin"
    env_bin.mkdir(parents=True)
    python = env_bin / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))

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
async def test_run_shell_rejects_scraping_cams_ads_pages(monkeypatch):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    async def fail_if_called(self, *args, **kwargs):
        raise AssertionError("run_shell should reject covered CAMS/ADS pages before execution")

    monkeypatch.setattr(Runtime, "run_subprocess_streaming", fail_if_called)

    result = await builtin_tools.run_shell(
        "curl -s -L https://ads.atmosphere.copernicus.eu/datasets/"
        "cams-global-atmospheric-composition-forecasts?tab=download | head -200",
        "inspect CAMS page",
    )

    assert result["status"] == "error"
    assert result["covered_data_source"] is True
    assert result["suggested_tools"] == ["search_cams_variables", "download_cams"]
    assert "不要用 run_shell/curl/wget" in result["message"]


@pytest.mark.asyncio
async def test_run_shell_rejects_ads_url_hidden_inside_python(monkeypatch):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    async def fail_if_called(self, *args, **kwargs):
        raise AssertionError("run_shell should reject ADS URLs before execution")

    monkeypatch.setattr(Runtime, "run_subprocess_streaming", fail_if_called)

    result = await builtin_tools.run_shell(
        "python3 -c \"url='https://ads.atmosphere.copernicus.eu/api/retrieve/v1/"
        "datasets/cams-global-atmospheric-composition-forecasts'; print(url)\"",
        "probe ADS API",
    )

    assert result["status"] == "error"
    assert result["covered_data_source"] is True
    assert result["suggested_tools"] == ["search_cams_variables", "download_cams"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        (
            "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
            "gfs.20260728/00/atmos/gfs.t00z.pgrb2.0p25.f000"
        ),
        (
            "https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
            "gfs.20260728/00/atmos/gfs.t00z.pgrb2.0p25.f000"
        ),
    ],
)
async def test_run_shell_rejects_direct_gfs_download(monkeypatch, url):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    async def fail_if_called(self, *args, **kwargs):
        raise AssertionError("run_shell should reject direct GFS downloads before execution")

    monkeypatch.setattr(Runtime, "run_subprocess_streaming", fail_if_called)

    result = await builtin_tools.run_shell(
        f"curl -L -o gfs.grib2 {url}",
        "download GFS directly",
    )

    assert result["status"] == "error"
    assert result["covered_data_source"] is True
    assert result["suggested_tools"] == ["inspect_gfs_inventory", "download_gfs"]
    assert "自动切换受控传输后端" in result["message"]


@pytest.mark.asyncio
async def test_run_shell_rejects_cdsapi_download_code(monkeypatch):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    async def fail_if_called(self, *args, **kwargs):
        raise AssertionError("run_shell should reject cdsapi download code before execution")

    monkeypatch.setattr(Runtime, "run_subprocess_streaming", fail_if_called)

    result = await builtin_tools.run_shell(
        "python3 - <<'PY'\n"
        "import cdsapi\n"
        "client = cdsapi.Client(url='https://ads.atmosphere.copernicus.eu/api', "
        "key='ee3a913f-03e7-4c83-a3b9-ed422aa0e091')\n"
        "client.retrieve('cams-global-atmospheric-composition-forecasts', {})\n"
        "PY",
        "manual cams download",
    )

    assert result["status"] == "error"
    assert result["covered_download_code_blocked"] is True
    assert result["suggested_tools"] == ["download_cams", "download_era5"]
    assert "ee3a913f" not in result["command"]
    assert "key='***'" in result["command"]


@pytest.mark.asyncio
async def test_run_shell_rejects_urllib_download_for_covered_source(monkeypatch):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    async def fail_if_called(self, *args, **kwargs):
        raise AssertionError("run_shell should reject urllib covered-source download")

    monkeypatch.setattr(Runtime, "run_subprocess_streaming", fail_if_called)

    result = await builtin_tools.run_shell(
        'python3 -c "import urllib.request; '
        "urllib.request.urlopen('https://ads.atmosphere.copernicus.eu/api/retrieve/v1/"
        "datasets/cams-global-atmospheric-composition-forecasts')\"",
        "manual ads request",
    )

    assert result["status"] == "error"
    assert result["covered_download_code_blocked"] is True


@pytest.mark.asyncio
async def test_run_shell_rejects_secret_file_probe(monkeypatch):
    from aero.agent.runtime import Runtime
    from aero.toolbox import builtin_tools

    async def fail_if_called(self, *args, **kwargs):
        raise AssertionError("run_shell should reject secret file probes before execution")

    monkeypatch.setattr(Runtime, "run_subprocess_streaming", fail_if_called)

    result = await builtin_tools.run_shell(
        "cat ~/.aero/secrets.yaml 2>/dev/null || cat ~/.aerolytica/secrets.yaml 2>/dev/null",
        "inspect secret file",
    )

    assert result["status"] == "error"
    assert result["secret_access_blocked"] is True
    assert "check_ads_config" in result["suggested_tools"]


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
    root = tmp_path / "runtime"
    env_bin = root / "envs" / "aero-agent" / "bin"
    env_bin.mkdir(parents=True)
    python = env_bin / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    monkeypatch.setenv("AERO_RUNTIME_ROOT", str(root))
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
