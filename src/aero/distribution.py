"""Versioned user installs. Bootstrap-safe: imports only the standard library."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

DOWNLOAD_BASE = "https://aero.skyviewor.com/download"
CHECK_INTERVAL = 24 * 60 * 60
MAX_MANIFEST = 1024 * 1024
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.+-]*)?\Z")
DEFAULT_RUNTIME_PROFILE = {
    "environment": "production",
    "api_url": "https://api.aerolytica.skyviewor.team",
    "llm_url": "https://llm.aerolytica.skyviewor.team/v1",
    "download_base": DOWNLOAD_BASE,
}


class DistributionError(RuntimeError):
    """An install failed before it was safe to activate it."""


class InstallCommandError(DistributionError):
    """A redacted command failure, optionally eligible for registry failover."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def installed_runtime_profile() -> dict:
    try:
        profile = json.loads(Path(__file__).with_name("release_environment.json").read_text())
    except FileNotFoundError:
        return dict(DEFAULT_RUNTIME_PROFILE)
    except (OSError, ValueError) as exc:
        raise DistributionError("无法读取发行环境配置。") from exc
    return validate_runtime_profile(profile)


def install_environment() -> str:
    profile = installed_runtime_profile()
    name = os.environ.get("AERO_ENVIRONMENT", profile["environment"])
    if name not in {"production", "testing"}:
        raise DistributionError("AERO_ENVIRONMENT 只能为 production 或 testing。")
    if Path(__file__).with_name("release_environment.json").exists():
        if name != profile["environment"]:
            raise DistributionError("安装包所属环境与 AERO_ENVIRONMENT 不一致。")
    return name


def default_download_base() -> str:
    name = install_environment()
    override = os.environ.get("AERO_DOWNLOAD_BASE")
    if override:
        return override.rstrip("/")
    profile = installed_runtime_profile()
    if name != profile["environment"]:
        raise DistributionError("测试环境必须明确设置 AERO_DOWNLOAD_BASE。")
    return profile["download_base"]


def install_root() -> Path:
    name = install_environment()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        default = base / ("Aerolytica-Testing" if name == "testing" else "Aerolytica")
    else:
        default = Path.home() / (".aero-testing" if name == "testing" else ".aero")
    home = Path(os.environ.get("AERO_HOME", str(default))).expanduser()
    return Path(os.environ.get("AERO_INSTALL_ROOT", str(home / "client"))).expanduser()


def private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise DistributionError("安装目录不能是符号链接。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt" and path.stat().st_uid != os.getuid():
        raise DistributionError("安装目录不属于当前用户。")
    if os.name == "nt":
        from aero.core.windows_acl import protect_private_directory

        protect_private_directory(path)
    else:
        path.chmod(0o700)
    return path


def write_state(path: Path, value: dict) -> None:
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def install_lock():
    from aero.core.file_lock import lock_nonblocking, nofollow_flag

    root = private_dir(install_root())
    fd = os.open(root / "install.lock", os.O_CREAT | os.O_RDWR | nofollow_flag(), 0o600)
    try:
        try:
            lock_nonblocking(fd)
        except BlockingIOError as exc:
            raise DistributionError("另一个安装或更新正在进行。") from exc
        yield
    finally:
        os.close(fd)


def target_platform() -> str:
    machine = platform.machine().lower()
    arch = {
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "x86_64": "x86_64",
        "amd64": "x86_64",
    }.get(machine)
    system = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    if not arch or not system:
        raise DistributionError("目前仅支持 macOS/Linux 的 x86_64/ARM64 和 Windows x64。")
    if system == "windows" and arch != "x86_64":
        raise DistributionError("Windows 目前仅支持 x64。")
    if system == "linux" and platform.libc_ver()[0] != "glibc":
        raise DistributionError("目前不支持 musl/Alpine，请使用 glibc Linux。")
    return f"{system}-{arch}"


def artifact_url(base: str, path: str) -> str:
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise DistributionError("下载源必须是 HTTPS 地址。")
    if (
        not re.fullmatch(r"[A-Za-z0-9_./+\-]+", path)
        or path.startswith("/")
        or ".." in path.split("/")
    ):
        raise DistributionError("发行清单中的下载路径不安全。")
    return base.rstrip("/") + "/" + path


def download(url: str, destination: Path, *, sha256: str = "", limit: int = 300_000_000):
    if urllib.parse.urlparse(url).scheme != "https":
        raise DistributionError("拒绝非 HTTPS 下载。")
    if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise DistributionError("发行文件校验值格式错误。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    try:
        for attempt in range(3):
            try:
                digest = hashlib.sha256()
                size = 0
                request = urllib.request.Request(
                    url, headers={"User-Agent": "Aerolytica/installer"}
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    if urllib.parse.urlparse(response.geturl()).scheme != "https":
                        raise DistributionError("下载发生了不安全的重定向。")
                    with partial.open("wb") as handle:
                        while chunk := response.read(1024 * 1024):
                            size += len(chunk)
                            if size > limit:
                                raise DistributionError("下载文件超过大小限制。")
                            digest.update(chunk)
                            handle.write(chunk)
                if sha256 and digest.hexdigest() != sha256:
                    raise DistributionError("下载文件校验失败，未启用新版本。")
                os.replace(partial, destination)
                return
            except (OSError, urllib.error.URLError) as exc:
                if attempt == 2:
                    raise DistributionError("下载失败，请检查网络后重试。") from exc
                time.sleep(attempt + 1)
    finally:
        partial.unlink(missing_ok=True)


def canonical_manifest(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def validate_runtime_profile(profile: dict) -> dict:
    try:
        if profile["environment"] not in {"production", "testing"}:
            raise ValueError
        for name in ("api_url", "llm_url", "download_base"):
            url = profile[name]
            parsed = urllib.parse.urlsplit(url)
            if (
                not isinstance(url, str)
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or any(char.isspace() for char in url)
                or "\\" in url
            ):
                raise ValueError
        if not urllib.parse.urlsplit(profile["llm_url"]).path.rstrip("/").endswith("/v1"):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DistributionError(
            "发行环境配置无效：需要环境名及完整 HTTPS API、LLM、下载地址。"
        ) from exc
    return {key: profile[key].rstrip("/") for key in DEFAULT_RUNTIME_PROFILE}


def assert_release_environment(manifest: dict, expected: str) -> None:
    profile = manifest.get("runtime", DEFAULT_RUNTIME_PROFILE)
    if validate_runtime_profile(profile)["environment"] != expected:
        raise DistributionError("发行环境不匹配；禁止在生产与测试环境之间升级。")


def validate_manifest(manifest: dict) -> dict:
    try:
        if manifest["schema"] != 1 or not VERSION_PATTERN.fullmatch(manifest["version"]):
            raise ValueError
        if manifest["distribution"] != "aerolytica":
            raise ValueError
        if not re.fullmatch(r"3\.12\.\d+", manifest["python"]):
            raise ValueError
        if "runtime" in manifest:
            validate_runtime_profile(manifest["runtime"])
        for key in ("wheel", "requirements"):
            item = manifest[key]
            artifact_url(DOWNLOAD_BASE, item["path"])
            if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise ValueError
        if not isinstance(manifest["platforms"], dict) or not manifest["platforms"]:
            raise ValueError
        for item in manifest["platforms"].values():
            artifact_url(DOWNLOAD_BASE, item["path"])
            if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DistributionError("发行清单格式错误。") from exc
    return manifest


def verify_manifest(envelope: dict, keys: dict | None = None) -> dict:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if keys is None:
        keys = json.loads(Path(__file__).with_name("release_keys.json").read_text())
    try:
        manifest = validate_manifest(envelope["manifest"])
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(keys[envelope["key_id"]]))
        key.verify(
            base64.b64decode(envelope["signature"], validate=True), canonical_manifest(manifest)
        )
    except (KeyError, ValueError, TypeError, InvalidSignature) as exc:
        raise DistributionError("发行签名无法验证；请检查官方发行公钥配置。") from exc
    return manifest


def fetch_release(base: str | None = None) -> dict:
    base = base or default_download_base()
    with tempfile.TemporaryDirectory(prefix="aero-release-") as directory:
        path = Path(directory) / "stable.json"
        download(artifact_url(base, "stable.json"), path, limit=MAX_MANIFEST)
        try:
            envelope = json.loads(path.read_text())
        except (ValueError, OSError) as exc:
            raise DistributionError("无法读取发行清单。") from exc
    verify_manifest(envelope)
    return envelope


def _command_failure(output: str) -> tuple[str, bool]:
    """Classify without exposing arbitrary subprocess output, credentials or paths."""
    value = output.lower()
    if "hash mismatch" in value or "hashes do not match" in value:
        return "依赖 SHA256 校验失败", False
    if "no space left" in value or "disk quota exceeded" in value:
        return "磁盘空间不足", False
    if "permission denied" in value:
        return "文件访问权限不足", False
    status = re.search(
        r"\b(401|403|404|408|429|5\d\d)\s+(?:unauthorized|forbidden|not found|"
        r"request timeout|too many requests|[a-z ]*error|service unavailable|"
        r"gateway timeout|bad gateway)",
        value,
    )
    if status:
        return f"依赖源返回 HTTP {status[1]}", True
    if any(
        word in value
        for word in (
            "connection",
            "timed out",
            "timeout",
            "dns",
            "certificate",
            "tls",
            "ssl",
            "failed to fetch",
            "failed to download",
            "not found in the package registry",
            "no matching distribution",
        )
    ):
        return "依赖源连接失败、超时或缺少锁定的软件包", True
    if "no solution found" in value:
        return "锁定依赖无法解析", False
    return "子进程执行失败", False


def _run(
    command: list[str],
    *,
    timeout: int = 900,
    step: str = "准备客户端环境",
    env: dict[str, str] | None = None,
) -> None:
    environment = os.environ.copy()
    for name in ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "PYTHONHOME"):
        environment.pop(name, None)
    environment.update(env or {})
    try:
        result = subprocess.run(
            command,
            env=environment,
            check=False,
            timeout=timeout,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallCommandError(
            f"{step}超时（{timeout} 秒）；旧版本未被覆盖。", retryable=True
        ) from exc
    except OSError as exc:
        raise InstallCommandError(f"{step}无法启动；旧版本未被覆盖。") from exc
    if result.returncode:
        reason, retryable = _command_failure((result.stderr or "") + (result.stdout or ""))
        raise InstallCommandError(
            f"{step}失败（{reason}；退出码 {result.returncode}）；旧版本未被覆盖。",
            retryable=retryable,
        )


def _dependency_indexes() -> list[str | None]:
    # An explicitly configured registry must never silently send requests elsewhere.
    overrides = (
        "UV_DEFAULT_INDEX",
        "UV_INDEX_URL",
        "UV_INDEX",
        "UV_EXTRA_INDEX_URL",
        "UV_FIND_LINKS",
        "UV_NO_INDEX",
    )
    if any(os.environ.get(name) for name in overrides):
        return [None]
    region = os.environ.get("AERO_NETWORK_REGION")
    if not region:
        region = os.environ.get("TZ", "") or str(Path("/etc/localtime").resolve())
    mainland = region.lower() in {"cn", "china", "mainland", "mainland_china"} or any(
        city in region for city in ("Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin")
    )
    mirrors = ["https://mirrors.aliyun.com/pypi/simple", "https://pypi.org/simple"]
    return ["https://pypi.tuna.tsinghua.edu.cn/simple", *mirrors] if mainland else mirrors[::-1]


def _sync_dependencies(uv: Path, python: Path, requirements: Path) -> None:
    command = [
        str(uv),
        "pip",
        "sync",
        "--no-config",
        "--python",
        str(python),
        "--require-hashes",
        "--only-binary",
        ":all:",
        str(requirements),
    ]
    indexes = _dependency_indexes()
    for attempt, index in enumerate(indexes):
        # Bound each source attempt; do not multiply uv's internal HTTP retries.
        environment = {"UV_HTTP_TIMEOUT": "20", "UV_HTTP_RETRIES": "0"}
        if index:
            environment["UV_DEFAULT_INDEX"] = index
        try:
            _run(command, timeout=180, step="安装锁定依赖", env=environment)
            return
        except InstallCommandError as exc:
            if not exc.retryable or attempt == len(indexes) - 1:
                raise
            print(f"{exc} 正在切换备用依赖源…", file=sys.stderr, flush=True)


def interpreter_version() -> str:
    return ".".join(str(value) for value in sys.version_info[:3])


def stage_release(envelope: dict, uv: Path, base: str, *, bootstrap: bool = False) -> Path:
    """Build at its final path: moving venvs would break absolute shebangs."""
    manifest = validate_manifest(envelope["manifest"]) if bootstrap else verify_manifest(envelope)
    assert_release_environment(manifest, install_environment())
    if bootstrap and interpreter_version() != manifest["python"]:
        raise DistributionError("引导脚本与发行清单的 Python 版本不同，请重新下载官方安装脚本。")
    root = private_dir(install_root())
    version = manifest["version"]
    environment = root / "versions" / version
    python = environment / "bin" / "python"
    marker = environment / "aero-install.json"
    fingerprint = hashlib.sha256(canonical_manifest(manifest)).hexdigest()
    if environment.is_symlink():
        raise DistributionError("版本目录不能是符号链接。")
    if marker.exists():
        if json.loads(marker.read_text()).get("manifest_hash") != fingerprint:
            raise DistributionError("同一版本已存在不同产物，拒绝替换。")
        smoke(environment, version)
        return environment
    private_dir(environment)
    if current_environment() == environment.resolve():
        raise DistributionError("当前版本缺少安装记录；拒绝覆盖运行中的环境。")
    with tempfile.TemporaryDirectory(prefix="aero-install-", dir=root) as directory:
        files = Path(directory)
        wheel = files / Path(manifest["wheel"]["path"]).name
        requirements = files / "requirements.txt"
        for key, destination in (("wheel", wheel), ("requirements", requirements)):
            item = manifest[key]
            download(artifact_url(base, item["path"]), destination, sha256=item["sha256"])
        # The shell bootstrap has already prepared a private interpreter.
        if bootstrap:
            interpreter = Path(sys.executable).resolve()
        else:
            try:
                item = manifest["platforms"][target_platform()]
            except KeyError as exc:
                raise DistributionError("此发行版不支持当前平台。") from exc
            archive = files / "python.tar.gz"
            download(artifact_url(base, item["path"]), archive, sha256=item["sha256"])
            # Interpreter trees are immutable too: never extract over an active
            # interpreter shared by the old and new virtual environments.
            prefix = (
                root / "python" / f"{manifest['python']}-{target_platform()}-{item['sha256'][:16]}"
            )
            if prefix.is_symlink():
                raise DistributionError("私有解释器目录不能是符号链接。")
            if not prefix.exists():
                private_dir(prefix.parent)
                with tempfile.TemporaryDirectory(prefix=".python-", dir=prefix.parent) as temporary:
                    with tarfile.open(archive) as bundle:
                        bundle.extractall(temporary, filter="data")
                    os.rename(temporary, prefix)
                    # TemporaryDirectory tolerates an already moved directory.
            interpreter = prefix / "python" / "bin" / "python3.12"
        _run(
            [
                str(uv),
                "venv",
                "--no-config",
                "--clear",
                "--python",
                str(interpreter),
                str(environment),
            ],
            step="创建私有虚拟环境",
        )
        _sync_dependencies(uv, python, requirements)
        _run(
            [
                str(uv),
                "pip",
                "install",
                "--no-config",
                "--python",
                str(python),
                "--no-deps",
                str(wheel),
            ],
            step="安装客户端发行包",
        )
        signed = files / "release.json"
        signed.write_text(json.dumps(envelope))
        # Bootstrap initially trusts the HTTPS script, but activation requires
        # verification using the public key actually embedded in the new wheel.
        _run(
            [
                str(python),
                "-c",
                "import json,sys; from aero.distribution import verify_manifest; "
                "from aero.release_environment import distribution_profile; "
                "manifest=verify_manifest(json.load(open(sys.argv[1]))); "
                "assert distribution_profile() == manifest.get('runtime', distribution_profile())",
                str(signed),
            ],
            step="验证发行签名",
        )
        smoke(environment, version)
    write_state(
        marker,
        {
            "version": version,
            "manifest_hash": fingerprint,
            "base_url": base,
            "uv": str(uv.resolve()),
            "managed": True,
            "runtime": manifest.get("runtime", DEFAULT_RUNTIME_PROFILE),
        },
    )
    return environment


def smoke(environment: Path, version: str) -> None:
    _run(
        [
            str(environment / "bin" / "python"),
            "-c",
            "import sys; from importlib.metadata import version; "
            "assert version('aerolytica') == sys.argv[1]; "
            "import aero.cli.main; from importlib.resources import files; "
            "assert files('aero.server').joinpath('static/index.html').is_file(); "
            "assert files('aero.cli').joinpath('environment.yaml').is_file(); "
            "assert files('aero').joinpath('skills/builtin').is_dir()",
            version,
        ],
        timeout=60,
        step="检查客户端资源与依赖",
    )
    _run([str(environment / "bin/aero"), "version"], timeout=15, step="检查客户端启动")


def current_environment() -> Path | None:
    current = install_root() / "current"
    return current.resolve() if current.is_symlink() else None


def managed_install() -> bool:
    marker = Path(sys.prefix) / "aero-install.json"
    if (
        not marker.is_file()
        or Path(sys.prefix).resolve().parent != (install_root() / "versions").resolve()
    ):
        return False
    try:
        return json.loads(marker.read_text()).get("managed") is True
    except (OSError, ValueError):
        return False


def activate(environment: Path) -> None:
    root = private_dir(install_root())
    if environment.resolve().parent != (root / "versions").resolve():
        raise DistributionError("拒绝启用不属于本安装的环境。")
    old = current_environment()
    if old and old != environment.resolve():
        _replace_link(root / "previous", old)
    _replace_link(root / "current", environment.resolve())


def _replace_link(path: Path, target: Path) -> None:
    temporary = path.with_name(f".{path.name}-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install_launcher(bin_dir: Path) -> Path:
    """Only replace a previous Aero launcher; never overwrite an unrelated CLI."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    if bin_dir.is_symlink() or bin_dir.stat().st_uid != os.getuid():
        raise DistributionError("CLI 入口目录必须是当前用户拥有的真实目录。")
    name = install_environment()
    launcher = bin_dir / ("aero-testing" if name == "testing" else "aero")
    if launcher.exists() or launcher.is_symlink():
        try:
            owned = "AEROLYTICA_MANAGED_LAUNCHER" in launcher.read_text()
        except (OSError, ValueError, ImportError):
            owned = False
        if not owned:
            raise DistributionError(f"{launcher} 已被其他安装占用；请先迁移该入口。")
    root = shlex.quote(str(install_root().resolve()))
    content = (
        "#!/bin/sh\n# AEROLYTICA_MANAGED_LAUNCHER\n"
        f"export AERO_INSTALL_ROOT={root}\n"
        f"export AERO_ENVIRONMENT={shlex.quote(name)}\n"
        f"export AERO_HOME={shlex.quote(os.environ.get('AERO_HOME', str(install_root().parent)))}\n"
        "unset PYTHONHOME PYTHONPATH\n"
        'exec "$AERO_INSTALL_ROOT/current/bin/aero" "$@"\n'
    )
    temporary = launcher.with_name(f".aero-{os.getpid()}")
    try:
        temporary.write_text(content)
        temporary.chmod(0o755)
        os.replace(temporary, launcher)
    finally:
        temporary.unlink(missing_ok=True)
    return launcher
