"""Managed runtime installation and shell execution tools."""

# ruff: noqa: E501

import re
import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from aero.agent.runtime import _conda_roots
from aero.toolbox.paths import find_project_dir
from aero.toolbox.registry import register_tool
from aero.toolbox.runtime_manager import get_runtime_tool_manager


@register_tool(
    name="ensure_runtime_tools",
    description=(
        "按 conda-helper 流程安装缺失的运行时命令行工具到统一 aero-agent conda 环境。"
        "当 cdo、grib_to_netcdf、ncrcat、ncks、ncdump 等命令不存在时调用；"
        "优先使用 aero-agent 内的 mamba 加速依赖解析；如果 mamba 不存在，会先把 mamba 安装到 aero-agent。"
        "不要改用 Python 脚本绕过缺失工具。工具会创建/更新 aero-agent、软链接命令到 conda base bin，并验证命令可用。"
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
        return {
            "status": "error",
            "message": "未找到 conda，无法创建 aero-agent 运行时环境。",
            "missing_tools": missing,
            "verified": verified,
        }

    root = manager.conda_root_from_executable(conda)
    env_bin = root / "envs" / "aero-agent" / "bin"
    base_bin = root / "bin"

    emit_progress("正在检查 aero-agent 运行时环境")
    env_exists = await manager.conda_env_exists_async(conda, env)
    env_create_command = None
    if not env_exists:
        env_create_cmd = [
            conda,
            "create",
            "-n",
            "aero-agent",
            "-c",
            "conda-forge",
            "--override-channels",
            "python=3.12",
            "-y",
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

    emit_progress(f"正在激活 aero-agent 环境：{env_bin}")

    mamba_install_command = None
    mamba_install_error = None
    package_manager = str(env_bin / "mamba") if (env_bin / "mamba").exists() else None
    if package_manager is None:
        emit_progress("aero-agent 中未找到 mamba，正在安装 mamba 以加速依赖解析")
        mamba_install_cmd = [
            conda,
            "install",
            "-n",
            "aero-agent",
            "-c",
            "conda-forge",
            "--override-channels",
            "mamba",
            "-y",
        ]
        mamba_install_command = " ".join(mamba_install_cmd)
        try:
            mamba_install = await manager.run_command_async(mamba_install_cmd, env=env, timeout=900)
        except subprocess.TimeoutExpired:
            mamba_install_error = "mamba 安装超时。"
        except OSError as exc:
            mamba_install_error = f"mamba 安装命令启动失败：{exc}"
        else:
            if mamba_install.returncode != 0:
                mamba_install_error = "mamba 安装失败。"
            else:
                env = Runtime._build_exec_env()
                if (env_bin / "mamba").exists():
                    package_manager = str(env_bin / "mamba")
        if package_manager is None:
            if mamba_install_error is None:
                mamba_install_error = "mamba 安装完成但仍未找到 mamba 命令。"
            emit_progress("mamba 不可用，改用 conda 更新 aero-agent")
            package_manager = conda

    packages: list[str] = []
    linked_tools: list[str] = []
    for tool in requested:
        package, package_tools = manager.packages[tool]
        if package not in packages:
            packages.append(package)
        for package_tool in package_tools:
            if package_tool not in linked_tools:
                linked_tools.append(package_tool)

    install_cmd = (
        [
            package_manager,
            "install",
            "-p",
            str(env_bin.parent),
            "-c",
            "conda-forge",
            "--override-channels",
            *packages,
            "-y",
        ]
        if Path(package_manager).name == "mamba"
        else [
            package_manager,
            "install",
            "-n",
            "aero-agent",
            "-c",
            "conda-forge",
            "--override-channels",
            *packages,
            "-y",
        ]
    )
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

    base_bin.mkdir(parents=True, exist_ok=True)
    symlinks = []
    emit_progress("正在软链接运行时工具到 PATH")
    for tool in linked_tools:
        src = env_bin / tool
        dest = base_bin / tool
        if src.exists():
            if dest.exists() or dest.is_symlink():
                dest.unlink()
            dest.symlink_to(src)
            symlinks.append({"tool": tool, "path": str(dest), "target": str(src)})

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
        "mamba_install_command": mamba_install_command,
        "mamba_install_error": mamba_install_error,
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
        "运行时会自动把统一 conda 环境 ~/miniconda3/envs/aero-agent/bin 放到 PATH 前面，"
        "通常不需要手动 conda activate。"
        "凡是通过 run_shell 执行 python/python3/pip/pip3/python -m pip，都必须解析到 aero-agent；"
        "不要用 base、系统 Python 或绝对路径绕过该环境。"
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
    python_error = _python_runtime_error(command, env)
    if python_error:
        return python_error
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

    error_message = result.error or ""
    if not result.success and not error_message:
        if stderr.strip():
            error_message = stderr.strip().splitlines()[-1]
        else:
            error_message = f"命令退出码 {result.exit_code}"
    message = "命令执行完成" if result.success else f"命令执行失败：{error_message}"

    return {
        "status": "success" if result.success else "error",
        "message": message,
        "error": error_message if not result.success else "",
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
    if any(marker in lowered for marker in ("urllib.request", "urlopen(", "requests.post", "requests.get")):
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
    return None


def _is_covered_data_url(url: str) -> bool:
    return _is_ads_cams_url(url) or _is_cds_dataset_url(url)


def _is_ads_cams_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return host == "ads.atmosphere.copernicus.eu" and (
        "/datasets/cams-" in path or "/api/" in path
    )


def _is_cds_dataset_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return host == "cds.climate.copernicus.eu" and "/datasets" in path


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
    env_bins = [(root / "envs" / "aero-agent" / "bin").resolve() for root in _conda_roots(env)]
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
    project_dir = find_project_dir().resolve()
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
