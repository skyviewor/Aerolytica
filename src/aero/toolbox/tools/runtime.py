"""Managed runtime installation and shell execution tools."""

# ruff: noqa: E501

import ast
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from aero.core.network_region import detect_network_region
from aero.core.runtime_paths import runtime_bin_path, runtime_env_path, runtime_root
from aero.toolbox.registry import register_tool
from aero.toolbox.runtime_manager import get_runtime_tool_manager


@register_tool(
    name="ensure_runtime_tools",
    description=(
        "安装缺失的运行时命令行工具到 Aero 私有科学计算环境。"
        "当 cdo、grib_to_netcdf、ncrcat、ncks、ncdump 等命令不存在时调用；"
        "环境由 Aero 自带的 Micromamba 管理，不使用或修改用户的 Conda 环境。"
        "不要改用 Python 脚本绕过缺失工具；安装后会验证命令可用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "缺失命令名，如 ['cdo', 'grib_to_netcdf', 'ncrcat']",
            },
        },
        "required": ["tools"],
    },
    requires_confirmation=True,
)
async def ensure_runtime_tools(tools: list[str]) -> dict:
    """Install missing CLI tools into the unified aero-agent conda env."""
    from aero.agent.progress import emit_progress
    from aero.agent.runtime import Runtime

    manager = get_runtime_tool_manager()
    requested = [str(tool).strip() for tool in tools if str(tool).strip()]
    if not requested:
        return {"status": "error", "message": "tools 不能为空。"}

    unknown = [tool for tool in requested if tool not in manager.packages]
    if unknown:
        return {
            "status": "error",
            "message": f"暂不知道这些命令对应的 conda 包：{', '.join(unknown)}",
            "known_tools": sorted(manager.packages),
        }

    env = Runtime._build_exec_env()
    if detect_network_region() == "mainland_china":
        emit_progress("检测到中国大陆网络，将使用大陆 conda/mamba 镜像")
    ready, missing, verified = manager.tools_ready(requested, env)
    if ready:
        emit_progress("运行时工具已安装并通过验证，无需重复安装")
        return {
            "status": "success",
            "message": "运行时工具已准备好，无需重复安装。",
            "environment": "aero-agent",
            "already_ready": True,
            "requested_tools": requested,
            "verified": verified,
        }

    conda = manager.find_conda_executable(env)
    if conda is None:
        import asyncio

        from aero.cli.init_runtime import ensure_micromamba

        emit_progress("Aero 私有运行时不存在，正在重新下载托管 Micromamba")
        managed_micromamba = await asyncio.to_thread(ensure_micromamba)
        if managed_micromamba is None:
            return {
                "status": "error",
                "message": (
                    "Aero 托管 Micromamba 下载失败。未使用、也不会回退到用户的 "
                    "Conda/Mamba；请检查网络后重试或运行 aero setup。"
                ),
                "missing_tools": missing,
                "verified": verified,
            }
        conda = str(managed_micromamba)

    env_bin = runtime_bin_path()

    emit_progress("正在检查 aero-agent 运行时环境")
    env_exists = await manager.conda_env_exists_async(conda, env)
    env_create_command = None
    if not env_exists:
        env_create_cmd = [
            conda,
            "create",
            "--yes",
            "--root-prefix",
            str(runtime_root()),
            "--prefix",
            str(runtime_env_path()),
            "--channel",
            "conda-forge",
            "--override-channels",
            "python=3.12",
        ]
        env_create_command = " ".join(env_create_cmd)
        emit_progress(f"正在创建 aero-agent 环境：{env_create_command}")
        try:
            env_create = await manager.run_command_async(env_create_cmd, env=env, timeout=900)
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "message": "aero-agent 环境创建超时。",
                "command": env_create_command,
            }
        except OSError as exc:
            return {
                "status": "error",
                "message": f"aero-agent 环境创建命令启动失败：{exc}",
                "command": env_create_command,
            }
        if env_create.returncode != 0:
            return {
                "status": "error",
                "message": "aero-agent 环境创建失败。",
                "command": env_create_command,
                "stdout": env_create.stdout[-8000:],
                "stderr": env_create.stderr[-8000:],
            }
        env = Runtime._build_exec_env()

    emit_progress(f"正在使用 Aero 私有环境：{env_bin}")
    package_manager = conda

    packages: list[str] = []
    for tool in requested:
        package, _package_tools = manager.packages[tool]
        if package not in packages:
            packages.append(package)

    install_cmd = [
        package_manager,
        "install",
        "--yes",
        "--root-prefix",
        str(runtime_root()),
        "--prefix",
        str(runtime_env_path()),
        "--channel",
        "conda-forge",
        "--override-channels",
        *packages,
    ]
    emit_progress(f"正在安装运行时工具：{' '.join(install_cmd)}")
    try:
        install = await manager.run_command_async(install_cmd, env=env, timeout=900)
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "message": "运行时工具安装超时。",
            "command": " ".join(install_cmd),
        }
    except OSError as exc:
        return {
            "status": "error",
            "message": f"运行时工具安装命令启动失败：{exc}",
            "command": " ".join(install_cmd),
        }
    if install.returncode != 0:
        return {
            "status": "error",
            "message": "运行时工具安装失败。",
            "command": " ".join(install_cmd),
            "stdout": install.stdout[-8000:],
            "stderr": install.stderr[-8000:],
        }

    symlinks = []

    verify_env = Runtime._build_exec_env()
    verified = []
    missing = []
    for tool in requested:
        found = shutil.which(tool, path=verify_env.get("PATH"))
        if found:
            verified.append({"tool": tool, "path": found})
        else:
            missing.append(tool)

    if missing:
        return {
            "status": "error",
            "message": f"安装完成但仍未找到命令：{', '.join(missing)}",
            "packages": packages,
            "symlinks": symlinks,
            "verified": verified,
        }

    return {
        "status": "success",
        "message": "运行时工具已准备好，可以重试原命令。",
        "environment": "aero-agent",
        "package_manager": package_manager,
        "env_create_command": env_create_command,
        "mamba_install_command": None,
        "mamba_install_error": None,
        "packages": packages,
        "requested_tools": requested,
        "verified": verified,
        "symlinks": symlinks,
        "install_command": " ".join(install_cmd),
    }


@register_tool(
    name="run_shell",
    description=(
        "执行 shell 命令。适合调用成熟 CLI 工具下载远程文件或处理本地文件；"
        "命令默认直接在用户当前工作根目录执行，不要猜测目录或在命令前添加 cd。"
        "运行时会自动把当前环境的 Aero 私有科研环境 bin 目录放到 PATH 前面，"
        "通常不需要手动 conda activate。"
        "凡是通过 run_shell 执行 python/python3/pip/pip3/python -m pip，都必须解析到 aero-agent；"
        "不要用 base、系统 Python 或绝对路径绕过该环境。"
        "cnmaps 是 pip-only 包，绝不能放入 conda/mamba install；"
        "必须用 aero-agent 的 python -m pip install -U cnmaps。"
        "远程数据下载应优先用内置下载工具；CAMS/ADS、CDS/ERA5、GFS/NOMADS/AWS 等"
        "已有专用工具覆盖的数据源，不要用 curl/wget/head/grep 抓网页或 API 查参数。"
        "只有内置工具完全覆盖不了的数据源，才使用 curl、wget、aria2c 或数据源官方 CLI，"
        "不要跳过下载工具和 CLI 直接写 Python HTTP/Range/下载脚本。"
        "GRIB/GRIB2/NetCDF 的合并、转换、拼接、裁剪、平均、元数据编辑应优先使用"
        " CDO、NCO、eccodes、netcdf-c 等命令行工具；只有用户明确要求脚本、CLI 不适合，"
        "或已经尝试安装/执行 CLI 但失败时，才用 Python/cfgrib/xarray 脚本兜底。\n\n"
        "常用命令：\n"
        "  curl -L -C - -o file.grib2 URL              下载未被内置工具覆盖的远程文件\n"
        "  wget -c -O file.grib2 URL                  下载未被内置工具覆盖的远程文件\n"
        "  aria2c -c -x 8 -s 8 -o file.grib2 URL      多连接下载未覆盖的远程文件\n"
        "  cdo -f nc copy input.grib2 output.nc        GRIB 转 NetCDF\n"
        "  cdo mergetime input*.nc output.nc           按时间合并 NetCDF\n"
        "  ncrcat input*.nc output.nc                  拼接 NetCDF 记录维\n"
        "  grib_to_netcdf -o output.nc input.grib2     eccodes 转 NetCDF\n"
        "  ncdump -h file.nc                           查看 NetCDF 头信息\n\n"
        "如果命令会用到 CDO、NCO、eccodes、netcdf-c、GDAL 等受管数据工具，"
        "先调用 ensure_runtime_tools 安装/验证到统一 aero-agent 环境，然后再运行命令；"
        "不要只用 which 检查 base 环境里的同名命令。run_shell 会拒绝使用未纳入 aero-agent 的受管数据工具。"
        "缺少 CLI 本身不是跳到 Python 脚本的理由；先安装并尝试 CLI，再按需用脚本兜底。\n\n"
        "独立命令可并行调用多个 run_shell，依赖命令用 && 串联。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令",
            },
            "description": {
                "type": "string",
                "description": "简短描述（5-10 个词）",
            },
            "workdir": {
                "type": "string",
                "description": "工作目录，默认用户当前工作根目录；通常无需填写",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "超时毫秒，默认 120000（2 分钟）",
            },
        },
        "required": ["command", "description"],
    },
    requires_confirmation=True,
)
async def run_shell(
    command: str,
    description: str,
    workdir: str = ".",
    timeout_ms: int = 120000,
) -> dict:
    """Execute a shell command via subprocess."""
    from aero.agent.runtime import Runtime

    runtime = Runtime()
    manager = get_runtime_tool_manager()
    command, workdir, context_correction = _normalize_shell_context(command, workdir)
    env = runtime._build_exec_env()
    secrets_error = _secrets_shell_error(command)
    if secrets_error:
        return secrets_error
    covered_download_code_error = _covered_download_code_shell_error(command)
    if covered_download_code_error:
        return covered_download_code_error
    covered_data_error = _covered_data_shell_error(command)
    if covered_data_error:
        return covered_data_error
    cnmaps_install_error = _cnmaps_conda_install_error(command)
    if cnmaps_install_error:
        return cnmaps_install_error
    user_conda_error = _user_conda_runtime_error(command)
    if user_conda_error:
        return user_conda_error
    python_error = _python_runtime_error(command, env)
    if python_error:
        return python_error
    plotting_error = _scientific_plot_script_error(command, workdir)
    if plotting_error:
        return plotting_error
    managed_tools = manager.managed_tools_in_command(command)
    if managed_tools:
        ready, missing, verified = manager.tools_ready(managed_tools, env)
        if not ready:
            return {
                "status": "error",
                "tool_missing": True,
                "message": (
                    "命令需要使用受管数据工具，但这些命令尚未安装/验证到 aero-agent 环境："
                    f"{', '.join(missing)}。请先调用 ensure_runtime_tools 安装并验证，然后重试原命令。"
                ),
                "required_tools": managed_tools,
                "missing_tools": missing,
                "verified": verified,
                "suggested_tool": "ensure_runtime_tools",
            }
    result = await runtime.run_subprocess_streaming(
        command,
        workdir,
        timeout_ms,
        output_limit=_run_shell_output_limit(command),
    )

    out = result.stdout
    stderr = result.stderr
    out_truncated = False
    err_truncated = False

    limit = _run_shell_output_limit(command)
    if len(out) > limit:
        out = out[-limit:]
        out_truncated = True
    if len(stderr) > limit:
        stderr = stderr[-limit:]
        err_truncated = True

    no_matches = _search_has_no_matches(command, result)
    successful = result.success or no_matches
    error_message = result.error or ""
    if not successful and not error_message:
        if stderr.strip():
            error_message = stderr.strip().splitlines()[-1]
        else:
            error_message = f"命令退出码 {result.exit_code}"
    message = (
        "未找到匹配内容"
        if no_matches
        else ("命令执行完成" if successful else f"命令执行失败：{error_message}")
    )

    return {
        "status": "success" if successful else "error",
        "message": message,
        "search_no_matches": no_matches,
        "error": error_message if not successful else "",
        "command": command,
        "workdir": workdir,
        "context_correction": context_correction,
        "exit_code": result.exit_code,
        "stdout": out,
        "stderr": stderr,
        "stdout_bytes": result.stdout_bytes,
        "stderr_bytes": result.stderr_bytes,
        "output_truncated": out_truncated
        or result.stdout_bytes > len(out.encode(errors="replace")),
        "stderr_truncated": err_truncated
        or result.stderr_bytes > len(stderr.encode(errors="replace")),
        "duration_ms": result.duration_ms,
    }


def _has_multiple_subplots(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    constants = {}
    assignments = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            assignments[node.id] = assignments.get(node.id, 0) + 1
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and assignments.get(target.id) == 1:
                    constants[target.id] = node.value.value

    def dimension(value):
        if isinstance(value, ast.Constant):
            return value.value if type(value.value) is int else None
        if isinstance(value, ast.Name):
            candidate = constants.get(value.id)
            return candidate if type(candidate) is int else None
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else (node.func.id if isinstance(node.func, ast.Name) else "")
        )
        if name != "subplots":
            continue
        if any(isinstance(arg, ast.Starred) for arg in node.args) or any(
            kw.arg is None for kw in node.keywords
        ):
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords}
        rows = (
            dimension(keywords["nrows"])
            if "nrows" in keywords
            else (dimension(node.args[0]) if node.args else 1)
        )
        columns = (
            dimension(keywords["ncols"])
            if "ncols" in keywords
            else (dimension(node.args[1]) if len(node.args) > 1 else 1)
        )
        if (
            rows is not None
            and columns is not None
            and rows > 0
            and columns > 0
            and rows * columns > 1
        ):
            return True
    return False


def _search_has_no_matches(command: str, result) -> bool:
    if result.exit_code != 1 or result.stderr.strip() or result.stdout.strip() or result.error:
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|;&<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    if not tokens or Path(tokens[0]).name not in {"grep", "rg"}:
        return False
    if any(token in {";", "&&", "||", "&", ">", ">>", "<", "<<"} for token in tokens):
        return False
    if "|" in tokens:
        index = tokens.index("|")
        return (
            tokens.count("|") == 1
            and index + 1 < len(tokens)
            and Path(tokens[index + 1]).name == "head"
        )
    return True


def _scientific_plot_script_error(command: str, workdir: str) -> dict | None:
    """Block unsafe or incomplete Cartopy plotting scripts before run.

    Skills guide model behaviour, but a generated source file is the last safe
    point to stop a visually incomplete scientific figure from being exported.
    The tight-bbox check applies to every local Cartopy contour map: its export
    bounds are not reliable with Cartopy transforms, colorbars, or inset axes.
    The remaining checks target multi-panel axes loops.
    """
    try:
        command_parts = shlex.split(command)
    except ValueError:
        return None

    python_indices = [
        index
        for index, part in enumerate(command_parts[:-1])
        if Path(part).name in {"python", "python3"}
    ]
    for index in python_indices:
        candidate = command_parts[index + 1]
        if candidate.startswith("-") or not candidate.endswith(".py"):
            continue
        script_path = Path(candidate)
        if not script_path.is_absolute():
            script_path = Path(workdir) / script_path
        if not script_path.is_file():
            continue
        try:
            source = script_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        is_cartopy_contour_map = "cartopy" in source and "contourf(" in source
        violations = _scientific_label_violations(source)
        if re.search(
            r"(?:savefig|print_(?:png|jpg|jpeg|tif|tiff))\s*\([^)]*"
            r"\blayout\s*=",
            source,
            flags=re.DOTALL,
        ):
            violations.append(
                "layout 只能在 plt.subplots()/plt.figure() 创建画布时设置，"
                "不能传给 savefig/print_png"
            )
        if is_cartopy_contour_map and re.search(r"bbox_inches\s*=\s*['\"]tight['\"]", source):
            violations.append(
                "Cartopy 地图不得使用 bbox_inches='tight'；请使用 "
                "layout='compressed'、合适的 figsize 和 colorbar 参数控制留白"
            )
        is_multi_panel = _has_multiple_subplots(source)
        if not (is_cartopy_contour_map and is_multi_panel):
            if violations:
                return {
                    "status": "error",
                    "scientific_plot_validation_failed": True,
                    "message": "科学绘图检查未通过：" + "；".join(violations),
                    "script_path": str(script_path),
                    "violations": violations,
                }
            continue

        if not re.search(r"extend\s*=\s*['\"]both['\"]", source):
            violations.append("每个有限 levels 的 contourf 必须显式使用 extend='both'")
        if not re.search(r"layout\s*=\s*['\"]compressed['\"]", source):
            violations.append(
                "固定比例 Cartopy 多子图必须使用 layout='compressed'，"
                "不能用 constrained layout 假装缩小子图间距"
            )
        elif re.search(r"(?:subplots_adjust|tight_layout)\s*\(", source):
            violations.append(
                "使用 layout='compressed' 后不得再调用 subplots_adjust/tight_layout；"
                "画布只能有一个布局所有者"
            )
        if re.search(
            r"\.suptitle\s*\([^)]*\by\s*=\s*(?:1(?:\.0*)?|1\.\d+|[2-9]\d*(?:\.\d+)?)",
            source,
            flags=re.DOTALL,
        ):
            violations.append(
                "总标题不得用 y>=1 放到画布外；省略 y，让布局引擎为 suptitle 预留空间"
            )
        if not re.search(r"\.gridlines\s*\(", source):
            violations.append("每个面板必须绘制轻量虚线经纬网")
        elif not all(
            re.search(pattern, source)
            for pattern in (
                r"linestyle\s*=\s*['\"]--['\"]",
                r"xlocs\s*=",
                r"ylocs\s*=",
            )
        ):
            violations.append("经纬网必须指定 xlocs/ylocs，并使用 linestyle='--'")
        if "ccrs.PlateCarree" in source:
            if re.search(r"gridlines\s*\([^)]*draw_labels\s*=\s*True", source):
                violations.append(
                    "矩形 PlateCarree 多子图不得依赖 Gridliner 绘制标签；"
                    "请用普通 GeoAxes 刻度，Gridliner 只绘制虚线"
                )
            if not all(
                re.search(pattern, source)
                for pattern in (
                    r"\.set_xticks\s*\(",
                    r"\.set_yticks\s*\(",
                    r"LongitudeFormatter\s*\(",
                    r"LatitudeFormatter\s*\(",
                    r"labelleft\s*=",
                    r"labelbottom\s*=",
                )
            ):
                violations.append(
                    "PlateCarree 多子图必须保留外侧经纬度标签："
                    "底行显示 LongitudeFormatter 刻度，左列显示 "
                    "LatitudeFormatter 刻度"
                )
        if not re.search(r"assert_artists_inside_canvas\s*\(\s*fig\s*\)", source):
            violations.append(
                "多子图必须在导出前调用 assert_artists_inside_canvas(fig)，"
                "用实际渲染边界检查标题碰撞及标题、坐标标签和色标是否超出画布"
            )

        if violations:
            return {
                "status": "error",
                "scientific_plot_validation_failed": True,
                "message": (
                    "多子图 Cartopy 科学绘图脚本未通过执行前检查："
                    + "；".join(violations)
                    + "。请先编辑脚本并重新执行，不能交付不完整的图。"
                ),
                "script_path": str(script_path),
                "violations": violations,
            }
    return None


def _scientific_label_violations(source: str) -> list[str]:
    """Find deterministic MathText escaping mistakes in plot labels."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    violations: list[str] = []
    label_methods = {"set_label", "set_xlabel", "set_ylabel"}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in label_methods
            and node.args
        ):
            continue
        label_node = node.args[0]
        if not (isinstance(label_node, ast.Constant) and isinstance(label_node.value, str)):
            continue
        label = label_node.value
        if label.count("$") % 2:
            violations.append(
                f"第 {node.lineno} 行的 {node.func.attr} 含有未配对的 `$`；"
                "完整数学单位必须放在同一对 `$...$` 中"
            )
        math_segments = label.split("$")
        text_segments = math_segments[::2]
        math_commands = (r"\mathrm", r"\,", r"\frac", r"\cdot", r"\times")
        if any(command in segment for segment in text_segments for command in math_commands) or any(
            re.search(r"(?<!\\)[_^]\{", segment) for segment in text_segments
        ):
            violations.append(
                f"第 {node.lineno} 行的 {node.func.attr} 把 MathText 命令放在了 "
                "`$...$` 外；请把完整单位写成例如 "
                'r"PVU ($10^{-6}\\,\\mathrm{K\\,m^2\\,kg^{-1}\\,s^{-1}}$)"'
            )
        if "\\\\" in label and any(
            marker in label for marker in ("\\\\mathrm", "\\\\,", "\\\\^", "\\\\_", "\\\\frac")
        ):
            violations.append(
                f"第 {node.lineno} 行的 {node.func.attr} 生成了双反斜杠；"
                '原始字符串 r"..." 中的 MathText 命令只能使用单反斜杠'
            )
        if re.search(r"\bPVU\b", label, flags=re.IGNORECASE):
            if re.search(r"(?<!1)0\^\{\s*-?6\s*\}", label):
                violations.append(
                    f"第 {node.lineno} 行的 {node.func.attr} 把 PVU 的 "
                    "`10^{-6}` 误写成了 `0^{-6}`"
                )
            canonical_pvu_unit = r"$10^{-6}\,\mathrm{K\,m^2\,kg^{-1}\,s^{-1}}$"
            if canonical_pvu_unit not in label:
                violations.append(
                    f"第 {node.lineno} 行的 {node.func.attr} 的 PVU 单位格式不规范；"
                    "请直接使用 "
                    'r"PVU ($10^{-6}\\,\\mathrm{K\\,m^2\\,kg^{-1}\\,s^{-1}}$)"'
                )
    return violations


def _covered_download_code_shell_error(command: str) -> dict | None:
    lowered = command.lower()
    if "cdsapi" in lowered or "ecmwf.datastores" in lowered:
        return {
            "status": "error",
            "covered_download_code_blocked": True,
            "message": (
                "不要用 run_shell 编写 cdsapi/ecmwf-datastores 下载代码。"
                "CAMS/ADS 与 CDS/ERA5 下载已有专用工具封装；"
                "请使用 download_cams 或 ERA5 下载工具。"
            ),
            "suggested_tools": ["download_cams", "download_era5"],
            "command": _redact_shell_command(command),
        }
    if any(
        marker in lowered
        for marker in ("urllib.request", "urlopen(", "requests.post", "requests.get")
    ):
        urls = _shell_urls(command)
        if any(_is_covered_data_url(url) for url in urls):
            return {
                "status": "error",
                "covered_download_code_blocked": True,
                "message": (
                    "不要用 run_shell 编写 Python HTTP/URL 下载代码访问已覆盖的数据源。"
                    "请使用对应专用下载工具；CAMS/ADS 用 download_cams，"
                    "CDS/ERA5 用 ERA5 下载工具。"
                ),
                "suggested_tools": ["download_cams", "download_era5"],
                "command": _redact_shell_command(command),
            }
    return None


def _secrets_shell_error(command: str) -> dict | None:
    lowered = command.lower()
    secret_markers = (
        ".aero/secrets.yaml",
        ".aero/secrets.yml",
        ".aerolytica/secrets.yaml",
        ".aerolytica/secrets.yml",
        ".aerolytica/keys.json",
        "aero_secrets_path",
        "secrets.yaml",
        "secrets.yml",
        "keys.json",
    )
    if not any(marker in lowered for marker in secret_markers):
        return None
    return {
        "status": "error",
        "secret_access_blocked": True,
        "message": (
            "不要用 run_shell 查找或读取 Aero 密钥文件。凭证状态属于内部配置，"
            "请使用对应配置检查工具：CAMS/ADS 用 check_ads_config，"
            "ERA5/CDS 用 check_cds_config，MERRA-2/Earthdata 用 check_earthdata_config。"
        ),
        "suggested_tools": ["check_ads_config", "check_cds_config", "check_earthdata_config"],
        "command": _redact_shell_command(command),
    }


def _cnmaps_conda_install_error(command: str) -> dict | None:
    """Keep pip-only cnmaps packages out of conda/mamba transactions."""
    has_cnmaps = re.search(r"(?<![\w-])cnmaps(?:-data)?(?![\w-])", command, re.IGNORECASE)
    conda_install = re.search(
        r"(?:^|[\s;&|])(?:[^\s;&|]*/)?(?:conda|mamba|micromamba)"
        r"\s+(?:install|create)\b",
        command,
        re.IGNORECASE,
    )
    if not has_cnmaps or not conda_install:
        return None
    pip_command = f"{runtime_bin_path() / 'python'} -m pip install -U cnmaps"
    return {
        "status": "error",
        "cnmaps_conda_install_blocked": True,
        "message": (
            "cnmaps 是 pip-only 包，不能通过 conda 或 mamba 安装。"
            "请从 conda/mamba 包列表中移除 cnmaps；其他依赖可继续用 conda/mamba，"
            "然后单独使用 aero-agent 的 Python 通过 pip 安装 cnmaps。"
        ),
        "suggested_command": pip_command,
        "command": _redact_shell_command(command),
    }


def _user_conda_runtime_error(command: str) -> dict | None:
    """Reject shell access to user Conda/Mamba installations."""
    package_manager = re.search(
        r"(?:^|[\s;&|])(?P<executable>[^\s;&|]*(?:conda|mamba|micromamba))"
        r"\s+(?:create|install|env\s+update)\b",
        command,
        re.IGNORECASE,
    )
    if package_manager is None:
        return None

    executable = package_manager.group("executable")
    from aero.core.runtime_paths import micromamba_path

    managed = str(micromamba_path())
    has_managed_scope = (
        executable == managed
        and f"--root-prefix {runtime_root()}" in command
        and f"--prefix {runtime_env_path()}" in command
    )
    if has_managed_scope:
        return None
    return {
        "status": "error",
        "user_conda_blocked": True,
        "message": (
            "禁止使用用户的 Conda/Mamba 或其 base 环境。缺少 aero-agent 环境时，"
            "请调用 ensure_runtime_tools；它会自动下载 Aero 托管 Micromamba，并在 "
            f"{runtime_env_path()} 重建隔离环境。"
        ),
        "suggested_tool": "ensure_runtime_tools",
        "command": _redact_shell_command(command),
    }


def _covered_data_shell_error(command: str) -> dict | None:
    urls = _shell_urls(command)
    if not urls:
        return None
    for url in urls:
        if _is_ads_cams_url(url):
            return {
                "status": "error",
                "covered_data_source": True,
                "message": (
                    "CAMS/ADS 数据源已有专用工具覆盖。不要用 run_shell/curl/wget 抓 ADS 网页或 API；"
                    "请先调用 search_cams_variables 或 search_dataset_variables 确认变量，"
                    "再调用 download_cams 下载。"
                ),
                "suggested_tools": ["search_cams_variables", "download_cams"],
                "command": _redact_shell_command(command),
            }
        if _is_cds_dataset_url(url):
            return {
                "status": "error",
                "covered_data_source": True,
                "message": (
                    "CDS/ERA5 数据源已有专用工具覆盖。不要用 run_shell/curl/wget 抓 CDS 网页；"
                    "请使用 search_cds_variables 或 ERA5 下载工具。"
                ),
                "suggested_tools": ["search_cds_variables", "download_era5"],
                "command": _redact_shell_command(command),
            }
        if _is_gfs_data_url(url):
            return {
                "status": "error",
                "covered_data_source": True,
                "message": (
                    "GFS/NOMADS/AWS 数据源已有专用工具覆盖。不要用 "
                    "run_shell/curl/wget 自行下载；GFS 适配器会在 TLS 失败时"
                    "自动切换受控传输后端，并继续按 .idx 精确下载所需字段。"
                ),
                "suggested_tools": ["inspect_gfs_inventory", "download_gfs"],
                "command": _redact_shell_command(command),
            }
    return None


def _is_covered_data_url(url: str) -> bool:
    return _is_ads_cams_url(url) or _is_cds_dataset_url(url) or _is_gfs_data_url(url)


def _is_ads_cams_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return host == "ads.atmosphere.copernicus.eu" and ("/datasets/cams-" in path or "/api/" in path)


def _is_cds_dataset_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return host == "cds.climate.copernicus.eu" and "/datasets" in path


def _is_gfs_data_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    path = parsed.path.lower()
    if host == "nomads.ncep.noaa.gov":
        return "/gfs/" in path
    return host in {
        "noaa-gfs-bdp-pds.s3.amazonaws.com",
        "noaa-gfs-bdp-pds.s3.us-east-1.amazonaws.com",
    } and (path.startswith("/gfs.") or "/gfs." in path)


def _shell_urls(command: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s'\"<>]+", command):
        urls.append(match.group(0).rstrip(".,;)"))
    for part in re.split(r"(?:&&|\|\||;|\|)", command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        for word in words:
            if word.startswith(("http://", "https://")):
                urls.append(word.rstrip(".,;"))
    return list(dict.fromkeys(urls))


def _redact_shell_command(command: str) -> str:
    redacted = re.sub(
        r"(?i)(\b(?:key|api_key|token|password)\s*=\s*)['\"][^'\"]+['\"]",
        r"\1'***'",
        command,
    )
    redacted = re.sub(
        r"(?i)(\b(?:key|api_key|token|password)\s*[:=]\s*)[^\s,'\"}]+",
        r"\1***",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(authorization\s*:\s*(?:bearer|token)?\s*)[^\s'\";]+",
        r"\1***",
        redacted,
    )
    return redacted


def _python_runtime_error(command: str, env: dict[str, str]) -> dict | None:
    python_tokens = _python_command_tokens(command)
    if not python_tokens:
        return None
    env_bins = [runtime_bin_path().resolve()]
    failures: list[dict[str, str]] = []
    for token in python_tokens:
        executable = _resolve_python_executable(token, env)
        if executable is None:
            failures.append({"tool": token, "reason": "not_found"})
            continue
        resolved = executable.resolve()
        if not any(resolved.parent == env_bin for env_bin in env_bins):
            failures.append(
                {
                    "tool": token,
                    "path": str(executable),
                    "reason": "not_in_aero_agent",
                }
            )
    if not failures:
        return None
    return {
        "status": "error",
        "python_runtime_invalid": True,
        "message": (
            "run_shell 执行 Python 程序必须使用 aero-agent 环境中的 python/pip。"
            "请先创建或修复 aero-agent 环境，然后重试；不要使用 base、系统 Python 或绝对路径。"
        ),
        "command": command,
        "failures": failures,
    }


def _python_command_tokens(command: str) -> list[str]:
    tokens: list[str] = []
    for part in re.split(r"(?:&&|\|\||;|\|)", command):
        try:
            words = shlex.split(part)
        except ValueError:
            continue
        if not words:
            continue
        executable = Path(words[0]).name
        if executable in {"python", "python3", "pip", "pip3"} or re.fullmatch(
            r"python3\.\d+", executable
        ):
            tokens.append(words[0])
    return tokens


def _resolve_python_executable(token: str, env: dict[str, str]) -> Path | None:
    path = Path(token).expanduser()
    if path.is_absolute() or "/" in token:
        return path if path.exists() else None
    found = shutil.which(token, path=env.get("PATH"))
    return Path(found) if found else None


def _normalize_shell_context(command: str, workdir: str) -> tuple[str, str, str]:
    """Run relative commands from the workspace and discard a missing leading cd."""
    from aero.toolbox.paths import find_workspace_dir

    project_dir = find_workspace_dir().resolve()
    requested_workdir = Path(workdir).expanduser()
    if not requested_workdir.is_absolute():
        requested_workdir = project_dir / requested_workdir
    correction = ""
    if not requested_workdir.is_dir():
        correction = f"工作目录不存在，已改用当前工作根目录：{project_dir}"
        requested_workdir = project_dir

    leading_cd = re.match(
        r"^\s*cd\s+(?P<target>'[^']*'|\"[^\"]*\"|[^\s;&|]+)\s*&&\s*",
        command,
    )
    if leading_cd:
        target_token = leading_cd.group("target")
        try:
            target_text = shlex.split(target_token)[0]
        except (ValueError, IndexError):
            target_text = ""
        target = Path(target_text).expanduser() if target_text else Path()
        if target_text and not target.is_absolute():
            target = requested_workdir / target
        target_outside_project = (
            target_text and target.is_dir() and not target.resolve().is_relative_to(project_dir)
        )
        if target_text and (not target.is_dir() or target_outside_project):
            command = command[leading_cd.end() :]
            correction = f"命令中的目录无效，已在当前工作根目录执行：{project_dir}"
            requested_workdir = project_dir
    return command, str(requested_workdir), correction


def _run_shell_output_limit(command: str) -> int:
    compact = " ".join(command.split()).lower()
    install_patterns = (
        "pip install",
        "python -m pip install",
        "conda install",
        "mamba install",
        "pixi add",
    )
    if any(pattern in compact for pattern in install_patterns):
        return 8000
    return 20000
