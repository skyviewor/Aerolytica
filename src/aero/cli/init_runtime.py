"""Interactive runtime setup used by ``aero init``."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from aero.core.network_region import apply_package_mirrors, detect_network_region

ENV_NAME = "aero-agent"
CONDA_ENVIRONMENT_FILE = Path(__file__).with_name("environment.yaml")
PIP_REQUIREMENTS_FILE = Path(__file__).with_name("env-requirements.txt")


def setup_runtime() -> bool:
    """Ensure conda, the managed environment, mamba, and optional common packages."""
    region = detect_network_region()
    if region == "mainland_china":
        print("检测到中国大陆网络，pip 和 conda/mamba 将使用大陆镜像。")
    else:
        print("检测到全球网络，pip 和 conda/mamba 将使用默认软件源。")
    conda = find_conda()
    if conda is None:
        if not _confirm("未找到 conda。是否安装 Miniconda？[Y/n] ", default=True):
            print("已跳过 Miniconda 和 aero-agent 环境安装。")
            return False
        default_path = Path.home() / "miniconda3"
        raw_path = input(f"Miniconda 安装路径 [{default_path}]: ").strip()
        install_path = Path(raw_path).expanduser() if raw_path else default_path
        conda = install_miniconda(install_path)
        if conda is None:
            return False
    else:
        print(f"已找到 conda: {conda}")

    if not ensure_aero_agent(conda):
        return False

    _print_common_packages()
    if _confirm("是否预装以上科学计算常用包？[y/N] ", default=False):
        if not install_common_packages(conda):
            return False
    else:
        print("已跳过常用包预装，后续可按需安装。")
    return True


def find_conda() -> Path | None:
    found = shutil.which("conda")
    if found:
        return Path(found)
    candidates = (
        Path.home() / "miniconda3" / "bin" / "conda",
        Path.home() / "anaconda3" / "bin" / "conda",
        Path.home() / "mambaforge" / "bin" / "conda",
        Path.home() / "miniforge3" / "bin" / "conda",
    )
    return next((candidate for candidate in candidates if candidate.exists()), None)


def install_miniconda(install_path: Path) -> Path | None:
    try:
        url = _miniconda_installer_url()
    except RuntimeError as exc:
        print(f"无法安装 Miniconda: {exc}")
        return None

    print(f"正在下载 Miniconda: {url}")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            installer = Path(temp_dir) / "miniconda.sh"
            urllib.request.urlretrieve(url, installer)
            result = _run(["bash", str(installer), "-b", "-p", str(install_path)], timeout=1800)
    except (OSError, subprocess.TimeoutExpired, urllib.error.URLError) as exc:
        print(f"Miniconda 安装失败: {exc}")
        return None
    if result.returncode != 0:
        _print_command_error("Miniconda 安装失败", result)
        return None

    conda = install_path / "bin" / "conda"
    if not conda.exists():
        print(f"Miniconda 安装完成，但未找到 conda: {conda}")
        return None
    print(f"Miniconda 已安装: {install_path}")
    return conda


def ensure_aero_agent(conda: Path) -> bool:
    env_path = _conda_root(conda) / "envs" / ENV_NAME
    if env_path.exists():
        print(f"{ENV_NAME} 环境已存在。")
    else:
        print(f"正在创建 {ENV_NAME} 环境...")
        try:
            result = _run(
                [
                    str(conda),
                    "create",
                    "-n",
                    ENV_NAME,
                    "-c",
                    "conda-forge",
                    "--override-channels",
                    "python=3.12",
                    "-y",
                ],
                timeout=1800,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"{ENV_NAME} 环境创建失败: {exc}")
            return False
        if result.returncode != 0:
            _print_command_error(f"{ENV_NAME} 环境创建失败", result)
            return False

    mamba = env_path / "bin" / "mamba"
    if not mamba.exists():
        print(f"正在为 {ENV_NAME} 安装 mamba...")
        try:
            result = _run(
                [
                    str(conda),
                    "install",
                    "-n",
                    ENV_NAME,
                    "-c",
                    "conda-forge",
                    "--override-channels",
                    "mamba",
                    "-y",
                ],
                timeout=1800,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"mamba 安装失败: {exc}")
            return False
        if result.returncode != 0:
            _print_command_error("mamba 安装失败", result)
            return False
        if not mamba.exists():
            print(f"mamba 安装完成，但未找到命令: {mamba}")
            return False
    print(f"{ENV_NAME} 运行环境已准备好。")
    return True


def install_common_packages(conda: Path) -> bool:
    env_path = _conda_root(conda) / "envs" / ENV_NAME
    mamba = env_path / "bin" / "mamba"
    conda_command = [
        str(mamba),
        "env",
        "update",
        "--prefix",
        str(env_path),
        "--file",
        str(CONDA_ENVIRONMENT_FILE),
        "-y",
    ]
    print("正在通过 environment.yaml 安装 conda 科学计算常用包...")
    try:
        result = _run(conda_command, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"conda 科学计算常用包安装失败: {exc}")
        return False
    if result.returncode != 0:
        _print_command_error("conda 科学计算常用包安装失败", result)
        return False

    pip_command = [
        str(env_path / "bin" / "python"),
        "-m",
        "pip",
        "install",
        "-r",
        str(PIP_REQUIREMENTS_FILE),
    ]
    print("正在通过 env-requirements.txt 安装 pip 科学计算常用包...")
    try:
        result = _run(pip_command, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"pip 科学计算常用包安装失败: {exc}")
        return False
    if result.returncode != 0:
        _print_command_error("pip 科学计算常用包安装失败", result)
        return False
    if not initialize_mplfonts(env_path):
        return False
    print("科学计算常用包已安装。")
    return True


def initialize_mplfonts(env_path: Path) -> bool:
    """Initialize Matplotlib's default CJK fonts in the managed environment."""
    print("正在初始化 Matplotlib 中文字体...")
    for action in ("init", "updaterc"):
        command = [str(env_path / "bin" / "mplfonts"), action]
        try:
            result = _run(command, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"Matplotlib 中文字体初始化失败: {exc}")
            return False
        if result.returncode != 0:
            _print_command_error("Matplotlib 中文字体初始化失败", result)
            return False
    print("Matplotlib 中文字体已初始化。")
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
    dependencies = data.get("dependencies") or []
    return tuple(str(package) for package in dependencies if isinstance(package, str))


def _pip_packages() -> tuple[str, ...]:
    return tuple(
        line
        for raw_line in PIP_REQUIREMENTS_FILE.read_text().splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    )


def _miniconda_installer_url() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
        filename = f"Miniconda3-latest-MacOSX-{arch}.sh"
    elif system == "Linux":
        arch = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
        filename = f"Miniconda3-latest-Linux-{arch}.sh"
    else:
        raise RuntimeError(f"暂不支持自动安装 Miniconda: {system}")
    return f"https://repo.anaconda.com/miniconda/{filename}"


def _conda_root(conda: Path) -> Path:
    return conda.resolve().parent.parent


def _confirm(prompt: str, *, default: bool) -> bool:
    answer = input(prompt).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes", "是", "好", "安装"}


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=apply_package_mirrors(dict(os.environ)),
    )


def _print_command_error(message: str, result: subprocess.CompletedProcess[str]) -> None:
    detail = result.stderr.strip() or result.stdout.strip()
    print(f"{message}: {detail[-2000:]}")
