"""Install and maintain Aero's private scientific runtime."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from aero.core.network_region import apply_package_mirrors, detect_network_region
from aero.core.runtime_paths import (
    micromamba_path,
    runtime_env_path,
    runtime_python_path,
    runtime_root,
    save_runtime_root,
)
from aero.distribution import DistributionError, download

ENV_NAME = "aero-agent"
CONDA_ENVIRONMENT_FILE = Path(__file__).with_name("environment.yaml")
PIP_REQUIREMENTS_FILE = Path(__file__).with_name("env-requirements.txt")


def setup_runtime(
    *,
    full: bool = False,
    assume_yes: bool = False,
    china_mirror: bool = False,
) -> bool:
    """Prepare Aero's private Python 3.12 runtime, optionally with the full toolset."""
    previous_region = os.environ.get("AERO_NETWORK_REGION")
    if china_mirror:
        os.environ["AERO_NETWORK_REGION"] = "mainland_china"
    try:
        if not _select_runtime_root(assume_yes=assume_yes):
            print("已取消运行时安装。")
            return False
        region = detect_network_region()
        source = "大陆镜像" if region == "mainland_china" else "默认软件源"
        print(f"正在准备 Aero 私有运行时（{source}）: {runtime_root()}")

        micromamba = ensure_micromamba()
        if micromamba is None or not ensure_aero_agent(micromamba):
            return False

        if full:
            return install_common_packages(micromamba)

        print("基础运行时已就绪；科学计算工具将在使用时按需安装。")
        print("如需一次性预装完整工具集，请运行: aero setup --full")
        return True
    finally:
        if china_mirror:
            if previous_region is None:
                os.environ.pop("AERO_NETWORK_REGION", None)
            else:
                os.environ["AERO_NETWORK_REGION"] = previous_region


def _select_runtime_root(*, assume_yes: bool) -> bool:
    """Offer an interactive, persistent runtime location selection."""
    current = runtime_root().expanduser().resolve()
    if assume_yes or not sys.stdin.isatty():
        return True

    print("Aero 运行时目录将由 Aero 专用；`aero runtime clean` 会删除整个目录。")
    entered = input(f"运行时安装路径 [{current}]（直接回车使用此路径）: ").strip()
    if entered:
        selected = Path(os.path.expandvars(entered)).expanduser().resolve()
    else:
        selected = current
    if selected.exists() and not selected.is_dir():
        print(f"所选路径不是目录: {selected}")
        return False
    if not _confirm(f"将在 {selected} 安装 Aero 私有运行时，继续？[Y/n] ", default=True):
        return False

    save_runtime_root(selected)
    # Make the selection effective immediately even if a parent process supplied
    # an older AERO_RUNTIME_ROOT value.
    os.environ["AERO_RUNTIME_ROOT"] = str(selected)
    return True


def ensure_micromamba() -> Path | None:
    executable = micromamba_path()
    if executable.exists():
        print(f"已找到 Aero Micromamba: {executable}")
        return executable

    try:
        url = _micromamba_archive_url()
    except RuntimeError as exc:
        print(f"无法安装 Micromamba: {exc}")
        return None

    print(f"正在下载 Micromamba: {url}")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "micromamba.tar.bz2"
            download(url, archive, limit=30_000_000)
            with tarfile.open(archive, "r:bz2") as bundle:
                member = next(
                    (item for item in bundle.getmembers() if item.name == "bin/micromamba"),
                    None,
                )
                if member is None or not member.isfile() or member.size > 30_000_000:
                    raise RuntimeError("下载包中未找到 bin/micromamba")
                extracted = bundle.extractfile(member)
                if extracted is None:
                    raise RuntimeError("无法读取 Micromamba 可执行文件")
                executable.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".micromamba-", dir=executable.parent)
                try:
                    with os.fdopen(fd, "wb") as destination:
                        shutil.copyfileobj(extracted, destination)
                        destination.flush()
                        os.fsync(destination.fileno())
                    Path(temporary).chmod(0o755)
                    os.replace(temporary, executable)
                finally:
                    Path(temporary).unlink(missing_ok=True)
    except (
        OSError,
        RuntimeError,
        DistributionError,
        tarfile.TarError,
        urllib.error.URLError,
    ) as exc:
        print(f"Micromamba 安装失败: {exc}")
        return None

    print(f"Micromamba 已安装: {executable}")
    return executable


def ensure_aero_agent(micromamba: Path) -> bool:
    env_path = runtime_env_path()
    python = runtime_python_path()
    if python.exists():
        print(f"{ENV_NAME} 环境已存在: {env_path}")
        return True

    print(f"正在创建 {ENV_NAME} Python 3.12 环境...")
    command = [
        str(micromamba),
        "create",
        "--yes",
        "--root-prefix",
        str(runtime_root()),
        "--prefix",
        str(env_path),
        "--channel",
        "conda-forge",
        "--override-channels",
        "python=3.12",
    ]
    try:
        result = _run(command, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{ENV_NAME} 环境创建失败: {exc}")
        return False
    if result.returncode != 0:
        _print_command_error(f"{ENV_NAME} 环境创建失败", result)
        return False
    if not python.exists():
        print(f"环境创建完成，但未找到 Python: {python}")
        return False
    print(f"{ENV_NAME} 运行环境已准备好。")
    return True


def install_common_packages(micromamba: Path) -> bool:
    env_path = runtime_env_path()
    command = [
        str(micromamba),
        "env",
        "update",
        "--yes",
        "--root-prefix",
        str(runtime_root()),
        "--prefix",
        str(env_path),
        "--file",
        str(CONDA_ENVIRONMENT_FILE),
    ]
    print("正在安装完整科学计算工具集...")
    try:
        result = _run(command, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"科学计算工具安装失败: {exc}")
        return False
    if result.returncode != 0:
        _print_command_error("科学计算工具安装失败", result)
        return False

    pip_command = [
        str(runtime_python_path()),
        "-m",
        "pip",
        "install",
        "-r",
        str(PIP_REQUIREMENTS_FILE),
    ]
    print("正在安装 pip 扩展（cnmaps 始终通过 pip 安装）...")
    try:
        result = _run(pip_command, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"pip 扩展安装失败: {exc}")
        return False
    if result.returncode != 0:
        _print_command_error("pip 扩展安装失败", result)
        return False
    if not initialize_mplfonts(env_path):
        return False
    print("完整科学计算工具集已安装。")
    return True


def initialize_mplfonts(env_path: Path) -> bool:
    print("正在初始化 Matplotlib 中文字体...")
    executable = env_path / "bin" / "mplfonts"
    for action in ("init", "updaterc"):
        try:
            result = _run([str(executable), action], timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"Matplotlib 中文字体初始化失败: {exc}")
            return False
        if result.returncode != 0:
            _print_command_error("Matplotlib 中文字体初始化失败", result)
            return False
    print("Matplotlib 中文字体已初始化。")
    return True


def runtime_diagnostics() -> tuple[bool, list[tuple[str, bool, str]]]:
    """Return human-readable health checks for ``aero doctor``."""
    checks: list[tuple[str, bool, str]] = []
    micromamba = micromamba_path()
    checks.append(("Micromamba", micromamba.exists(), str(micromamba)))
    python = runtime_python_path()
    checks.append(("Python", python.exists(), str(python)))
    if python.exists():
        try:
            result = _run(
                [str(python), "-c", "import platform; print(platform.python_version())"],
                timeout=30,
                capture=True,
            )
            version = result.stdout.strip()
            valid = result.returncode == 0 and version.startswith("3.12.")
            checks.append(("Python 版本", valid, version or "无法读取"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append(("Python 版本", False, str(exc)))
    return all(item[1] for item in checks), checks


def clean_runtime(*, assume_yes: bool = False) -> bool:
    root = runtime_root()
    if not root.exists():
        print(f"Aero 私有运行时不存在，无需清理: {root}")
        return True
    should_delete = assume_yes or _confirm(
        f"将删除 Aero 私有运行时 {root}，继续？[y/N] ", default=False
    )
    if not should_delete:
        print("已取消。")
        return False
    shutil.rmtree(root)
    print(f"Aero 私有运行时已删除: {root}")
    return True


def _print_common_packages() -> None:
    print(f"Conda 包（{CONDA_ENVIRONMENT_FILE.name}）：")
    for package in _conda_packages():
        print(f"  {package}")
    print(f"Pip 包（{PIP_REQUIREMENTS_FILE.name}）：")
    for package in _pip_packages():
        print(f"  {package}")


def _conda_packages() -> tuple[str, ...]:
    data = yaml.safe_load(CONDA_ENVIRONMENT_FILE.read_text()) or {}
    return tuple(
        str(package) for package in (data.get("dependencies") or []) if isinstance(package, str)
    )


def _pip_packages() -> tuple[str, ...]:
    return tuple(
        line
        for raw_line in PIP_REQUIREMENTS_FILE.read_text().splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    )


def _micromamba_archive_url() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        target = "osx-arm64" if machine in {"arm64", "aarch64"} else "osx-64"
    elif system == "Linux":
        target = "linux-aarch64" if machine in {"arm64", "aarch64"} else "linux-64"
    else:
        raise RuntimeError(f"暂不支持自动安装 Micromamba: {system}")
    return f"https://micro.mamba.pm/api/micromamba/{target}/latest"


def _confirm(prompt: str, *, default: bool) -> bool:
    answer = input(prompt).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes", "是", "好", "安装", "继续"}


def _run(
    command: list[str], *, timeout: int, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run an installer attached to the terminal so progress is visible live."""
    env = apply_package_mirrors(dict(os.environ))
    env["MAMBA_ROOT_PREFIX"] = str(runtime_root())
    return subprocess.run(
        command,
        timeout=timeout,
        check=False,
        env=env,
        **({"capture_output": True, "text": True} if capture else {}),
    )


def _print_command_error(message: str, result: subprocess.CompletedProcess[str]) -> None:
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    detail = stderr.strip() or stdout.strip()
    suffix = f": {detail[-2000:]}" if detail else f"（退出码 {result.returncode}）"
    print(f"{message}{suffix}")
