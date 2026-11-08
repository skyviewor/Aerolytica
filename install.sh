#!/usr/bin/env sh
# Install only the CLI; account/scientific onboarding remains aero setup.
set -eu
umask 077
fail() { printf '\n安装失败：%s\n' "$*" >&2; exit 1; }
AERO_VERSION="${AERO_VERSION:-}"
AERO_INSTALL_REVISION="${AERO_INSTALL_REVISION:-}"
AERO_ALLOW_DOWNGRADE="${AERO_ALLOW_DOWNGRADE:-0}"
AERO_REINSTALL="${AERO_REINSTALL:-0}"
INSTALL_PREFIX=""
INSTALL_BIN=""
AERO_INSTALL_YES="${AERO_INSTALL_YES:-0}"
AERO_SHELL_INIT="${AERO_SHELL_INIT:-auto}"
AERO_INSTALL_SHELL="${AERO_INSTALL_SHELL:-}"
AERO_SHELL_RC_FILE="${AERO_SHELL_RC_FILE:-}"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --version|--revision)
            [ "$#" -ge 2 ] || fail "$1 后需要指定值"
            case "$2" in ''|--*) fail "$1 后需要指定值" ;; esac
            case "$1" in --version) AERO_VERSION="$2" ;; --revision) AERO_INSTALL_REVISION="$2" ;; esac
            shift 2 ;;
        --allow-downgrade) AERO_ALLOW_DOWNGRADE=1; shift ;;
        --reinstall) AERO_REINSTALL=1; shift ;;
        --prefix|--bin-dir|--shell|--rc-file)
            [ "$#" -ge 2 ] || fail "$1 后需要指定值"
            case "$2" in ''|--*) fail "$1 后需要指定值" ;; esac
            case "$1" in
                --prefix) INSTALL_PREFIX="$2" ;;
                --bin-dir) INSTALL_BIN="$2" ;;
                --shell) AERO_INSTALL_SHELL="$2" ;;
                --rc-file) AERO_SHELL_RC_FILE="$2" ;;
            esac
            shift 2 ;;
        --yes|-y) AERO_INSTALL_YES=1; shift ;;
        --shell-init) AERO_SHELL_INIT=yes; shift ;;
        --no-shell-init) AERO_SHELL_INIT=no; shift ;;
        --help|-h)
            printf '%s\n' '用法：sh install.sh [--version 0.1.1] [--revision 完整测试提交SHA] [--allow-downgrade] [--reinstall]'
            printf '%s\n' '不指定版本则安装最新版；降级必须显式允许，不删除账号、项目或科研环境。'
            printf '%s\n' '路径：--prefix 绝对安装目录 --bin-dir 命令目录；终端：--shell zsh|bash|fish|sh --rc-file 配置文件'
            printf '%s\n' '--yes 使用默认路径；无人值守不改 shell，除非明确指定 --shell-init；--no-shell-init 永不修改 shell。'
            exit 0 ;;
        *) fail "未知安装参数：$1（使用 --help 查看）" ;;
    esac
done
AERO_VERSION="${AERO_VERSION#v}"
[ "$AERO_VERSION" != latest ] || AERO_VERSION=""
if [ -n "$AERO_VERSION" ]; then
    case "$AERO_VERSION" in *[!0-9a-zA-Z.+-]*|*..*) fail "版本号格式不正确" ;; esac
    printf '%s\n' "$AERO_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9.+-]*$' || fail "版本号格式不正确"
fi
if [ -n "$AERO_INSTALL_REVISION" ]; then
    [ -n "$AERO_VERSION" ] || fail "--revision 必须同时指定 --version"
    case "$AERO_INSTALL_REVISION" in *[!0-9a-f]*) fail "测试修订必须为40位小写十六进制提交 SHA" ;; esac
    [ "${#AERO_INSTALL_REVISION}" -eq 40 ] || fail "测试修订必须为40位完整提交 SHA"
fi
case "$AERO_ALLOW_DOWNGRADE:$AERO_REINSTALL" in 0:0|0:1|1:0|1:1) ;; *) fail "安装确认开关只能为0或1" ;; esac
if [ "$AERO_ALLOW_DOWNGRADE" = 1 ]; then
    [ -n "$AERO_VERSION" ] || fail "允许降级时必须明确指定 --version"
fi
if [ -n "${AERO_RELEASE_MANIFEST:-}" ]; then
    [ -z "$AERO_VERSION$AERO_INSTALL_REVISION" ] || fail "不能同时指定版本和 AERO_RELEASE_MANIFEST"
    case "$AERO_RELEASE_MANIFEST" in /*|*://*|*..*|*[!0-9a-zA-Z_./+-]*) fail "发行清单路径不安全" ;; esac
fi
export AERO_VERSION AERO_INSTALL_REVISION AERO_ALLOW_DOWNGRADE AERO_REINSTALL
AERO_ENVIRONMENT="${AERO_ENVIRONMENT:-production}"
AERO_DOWNLOAD_BASE="${AERO_DOWNLOAD_BASE:-https://aero.skyviewor.com/download}"
case "$AERO_ENVIRONMENT" in
    production) DEFAULT_AERO_HOME="$HOME/.aero"; AERO_COMMAND=aero ;;
    testing) DEFAULT_AERO_HOME="$HOME/.aero-testing"; AERO_COMMAND=aero-testing ;;
    *) printf '不支持的发行环境\n' >&2; exit 1 ;;
esac
if [ -n "$AERO_INSTALL_REVISION" ] && [ "$AERO_ENVIRONMENT" != testing ]; then
    fail "指定提交修订只适用于 testing 环境"
fi
AERO_HOME="${INSTALL_PREFIX:-${AERO_HOME:-$DEFAULT_AERO_HOME}}"
expand_install_path() {
    case "$1" in '~') printf '%s\n' "$HOME" ;; '~/'*) printf '%s/%s\n' "$HOME" "${1#\~/}" ;; *) printf '%s\n' "$1" ;; esac
}
AERO_HOME="$(expand_install_path "$AERO_HOME")"
# /dev/tty is independent of stdin, which may still contain the piped installer.
if [ "$AERO_INSTALL_YES" != 1 ] && [ -z "${CI:-}${GITHUB_ACTIONS:-}" ] &&
    ( : </dev/tty ) 2>/dev/null; then
    printf '请输入安装地址（绝对路径，回车使用默认值）[%s]: ' "$AERO_HOME" >/dev/tty
    IFS= read -r INSTALL_REPLY </dev/tty || fail "已取消安装"
    [ -z "$INSTALL_REPLY" ] || AERO_HOME="$(expand_install_path "$INSTALL_REPLY")"
    printf '将在 %s 安装客户端，原有其他目录不会自动迁移。继续？[Y/n] ' "$AERO_HOME" >/dev/tty
    IFS= read -r INSTALL_REPLY </dev/tty || fail "已取消安装"
    case "$INSTALL_REPLY" in ''|y|Y|yes|YES) ;; *) fail "已取消安装，未开始下载" ;; esac
fi
case "$AERO_HOME" in /*) ;; *) fail "安装主目录必须是绝对路径" ;; esac
[ -z "$INSTALL_PREFIX" ] || AERO_INSTALL_ROOT="$AERO_HOME/client"
AERO_INSTALL_ROOT="${AERO_INSTALL_ROOT:-$AERO_HOME/client}"
AERO_INSTALL_ROOT="$(expand_install_path "$AERO_INSTALL_ROOT")"
AERO_BIN_DIR="$(expand_install_path "${INSTALL_BIN:-${AERO_BIN_DIR:-$AERO_HOME/bin}}")"
case "$AERO_BIN_DIR" in /*) ;; *) fail "命令目录必须是绝对路径" ;; esac
case "$AERO_HOME$AERO_INSTALL_ROOT$AERO_BIN_DIR" in *:*|*'
'*) fail "安装路径不能包含冒号或换行" ;; esac
case "$AERO_SHELL_INIT" in auto|yes|no) ;; *) fail "shell 配置选项必须为 auto、yes 或 no" ;; esac
case "$AERO_INSTALL_YES" in 0|1) ;; *) fail "AERO_INSTALL_YES 只能为 0 或 1" ;; esac
AERO_PARENT_SHELL="$(ps -p "$PPID" -o comm= 2>/dev/null || true)"
AERO_PARENT_SHELL="${AERO_PARENT_SHELL##*/}"
AERO_PARENT_SHELL="${AERO_PARENT_SHELL#-}"
case "$AERO_PARENT_SHELL" in bash|zsh|fish) AERO_CURRENT_SHELL="$AERO_PARENT_SHELL" ;;
    *) AERO_CURRENT_SHELL="${SHELL:-}"; AERO_CURRENT_SHELL="${AERO_CURRENT_SHELL##*/}" ;;
esac
AERO_INSTALL_SHELL="${AERO_INSTALL_SHELL:-$AERO_CURRENT_SHELL}"
export AERO_INSTALL_YES AERO_SHELL_INIT AERO_INSTALL_SHELL AERO_SHELL_RC_FILE AERO_CURRENT_SHELL
export AERO_INSTALL_ROOT AERO_BIN_DIR AERO_HOME AERO_ENVIRONMENT
UV_VERSION="0.12.23"
PYTHON_VERSION="3.12.12"
PYTHON_BUILD="20251014"
case "$AERO_INSTALL_ROOT" in /*) ;; *) fail "AERO_INSTALL_ROOT 必须是绝对路径" ;; esac
[ ! -L "$AERO_INSTALL_ROOT" ] || fail "安装目录不能是符号链接"
mkdir -p "$AERO_INSTALL_ROOT"
chmod 700 "$AERO_INSTALL_ROOT"
case "$AERO_DOWNLOAD_BASE" in https://*) ;; *) fail "下载源必须使用 HTTPS" ;; esac
command -v curl >/dev/null 2>&1 || fail "需要 curl；无需预装 Python 或 Conda。"
case "$(uname -s):$(uname -m)" in
    Darwin:arm64|Darwin:aarch64) TARGET=aarch64-apple-darwin; PLATFORM=macos-aarch64 ;;
    Darwin:x86_64) TARGET=x86_64-apple-darwin; PLATFORM=macos-x86_64 ;;
    Linux:arm64|Linux:aarch64) TARGET=aarch64-unknown-linux-gnu; PLATFORM=linux-aarch64 ;;
    Linux:x86_64) TARGET=x86_64-unknown-linux-gnu; PLATFORM=linux-x86_64 ;;
    *) fail "目前仅支持 macOS/Linux 的 x86_64 和 ARM64。" ;;
esac
if [ "$(uname -s)" = Linux ]; then
    getconf GNU_LIBC_VERSION >/dev/null 2>&1 || fail "目前不支持 musl/Alpine。"
fi
case "${AERO_NETWORK_REGION:-${TZ:-$(readlink /etc/localtime 2>/dev/null || true)}}" in
    cn|china|mainland|mainland_china|*Asia/Shanghai|*Asia/Chongqing|*Asia/Harbin)
        # Pass the region, not an implicit index override: the verified installer
        # can then fail over between mirrors while respecting explicit UV_* settings.
        export AERO_NETWORK_REGION="${AERO_NETWORK_REGION:-mainland_china}" ;;
esac
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/aero-install.XXXXXX")"
trap 'rm -rf "$TEMP_DIR"' EXIT HUP INT TERM
fetch() {
    curl -fLsS --retry 3 --connect-timeout 10 --max-time 600 \
        --proto '=https' --proto-redir '=https' "$1" -o "$2"
}
fetch_optional() {
    OPTIONAL_STATUS="$(curl -LsS --retry 3 --connect-timeout 10 --max-time 600 \
        --proto '=https' --proto-redir '=https' -w '%{http_code}' "$1" -o "$2")" || fail "无法连接发行下载源"
    case "$OPTIONAL_STATUS" in
        200) return 0 ;;
        404|410) return 1 ;;
        *) fail "读取发行清单失败（HTTP ${OPTIONAL_STATUS}），未回退到其他版本" ;;
    esac
}
check_hash() {
    EXPECTED_HASH="$(awk 'NR == 1 {print $1}' "$TEMP_DIR/$1.sha256")"
    case "$EXPECTED_HASH" in ''|*[!0-9a-f]*) fail "安装校验值格式错误" ;; esac
    [ "${#EXPECTED_HASH}" -eq 64 ] || fail "安装校验值长度错误"
    if command -v sha256sum >/dev/null 2>&1; then
        ACTUAL_HASH="$(cd "$TEMP_DIR" && sha256sum "$1" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        ACTUAL_HASH="$(cd "$TEMP_DIR" && shasum -a 256 "$1" | awk '{print $1}')"
    else fail "缺少 sha256sum/shasum，无法验证安装文件。"; fi
    [ "$ACTUAL_HASH" = "$EXPECTED_HASH" ] || fail "安装文件校验失败：$1"
}
UV_DIR="$AERO_INSTALL_ROOT/bootstrap/uv-$UV_VERSION-$PLATFORM"
if [ ! -x "$UV_DIR/uv" ]; then
    printf '准备私有安装工具…\n'
    fetch "$AERO_DOWNLOAD_BASE/bootstrap/uv/$UV_VERSION/$TARGET.tar.gz" "$TEMP_DIR/uv.tar.gz"
    fetch "$AERO_DOWNLOAD_BASE/bootstrap/uv/$UV_VERSION/$TARGET.tar.gz.sha256" "$TEMP_DIR/uv.tar.gz.sha256"
    check_hash uv.tar.gz
    [ ! -e "$UV_DIR" ] || fail "私有安装工具目录不完整，请检查 $UV_DIR"
    mkdir -p "$AERO_INSTALL_ROOT/bootstrap"
    tar -xzf "$TEMP_DIR/uv.tar.gz" -C "$TEMP_DIR"
    mv "$TEMP_DIR/uv-$TARGET" "$UV_DIR"
    chmod 700 "$UV_DIR/uv"
fi
PYTHON_DIR="$AERO_INSTALL_ROOT/python/$PYTHON_VERSION-$PLATFORM"
PYTHON="$PYTHON_DIR/python/bin/python3.12"
if [ ! -x "$PYTHON" ]; then
    printf '准备私有 Python（不会修改系统 Python）…\n'
    fetch "$AERO_DOWNLOAD_BASE/bootstrap/python/$PYTHON_BUILD/$PLATFORM.tar.gz" "$TEMP_DIR/python.tar.gz"
    fetch "$AERO_DOWNLOAD_BASE/bootstrap/python/$PYTHON_BUILD/$PLATFORM.tar.gz.sha256" "$TEMP_DIR/python.tar.gz.sha256"
    check_hash python.tar.gz
    [ ! -e "$PYTHON_DIR" ] || fail "私有 Python 目录不完整，请检查 $PYTHON_DIR"
    mkdir -p "$AERO_INSTALL_ROOT/python" "$TEMP_DIR/python-stage"
    tar -xzf "$TEMP_DIR/python.tar.gz" -C "$TEMP_DIR/python-stage"
    mv "$TEMP_DIR/python-stage" "$PYTHON_DIR"
fi
unset PYTHONHOME PYTHONPATH
# The current signed release provides the installer coordinator, including for
# historical wheels whose original bootstrap predates version selection.
HAS_COORDINATOR=1
if ! fetch_optional "$AERO_DOWNLOAD_BASE/stable.json" "$TEMP_DIR/coordinator.json"; then
    HAS_COORDINATOR=0
    [ -n "$AERO_VERSION${AERO_RELEASE_MANIFEST:-}" ] || fail "尚未发布默认版本，请指定已发布的版本"
fi
if [ -n "$AERO_VERSION" ]; then
    if [ -n "$AERO_INSTALL_REVISION" ]; then
        SELECTED_MANIFEST="releases/$AERO_VERSION/revisions/$AERO_INSTALL_REVISION/release.json"
        fetch_optional "$AERO_DOWNLOAD_BASE/$SELECTED_MANIFEST" "$TEMP_DIR/stable.json" || fail "指定测试修订不存在或尚未发布"
    elif [ "$HAS_COORDINATOR" = 1 ] && "$PYTHON" -c 'import json,sys; sys.exit(json.load(open(sys.argv[1]))["manifest"]["version"] != sys.argv[2])' "$TEMP_DIR/coordinator.json" "$AERO_VERSION"; then
        cp "$TEMP_DIR/coordinator.json" "$TEMP_DIR/stable.json"
    else
        SELECTED_MANIFEST="releases/$AERO_VERSION/latest.json"
        if ! fetch_optional "$AERO_DOWNLOAD_BASE/$SELECTED_MANIFEST" "$TEMP_DIR/stable.json"; then
            # Releases published before per-version pointers used a flat layout.
            SELECTED_MANIFEST="releases/$AERO_VERSION/release.json"
            fetch_optional "$AERO_DOWNLOAD_BASE/$SELECTED_MANIFEST" "$TEMP_DIR/stable.json" || fail "指定版本不存在或尚未发布"
        fi
    fi
elif [ -n "${AERO_RELEASE_MANIFEST:-}" ]; then
    fetch "$AERO_DOWNLOAD_BASE/$AERO_RELEASE_MANIFEST" "$TEMP_DIR/stable.json"
else
    cp "$TEMP_DIR/coordinator.json" "$TEMP_DIR/stable.json"
fi
[ "$HAS_COORDINATOR" = 1 ] || cp "$TEMP_DIR/stable.json" "$TEMP_DIR/coordinator.json"
"$PYTHON" - "$TEMP_DIR/stable.json" "$TEMP_DIR" "$TEMP_DIR/coordinator.json" <<'PY'
import json, os, re, sys
from pathlib import Path
root = Path(sys.argv[2])
manifest = json.loads(Path(sys.argv[1]).read_text())["manifest"]
if manifest.get("distribution") != "aerolytica" or manifest.get("schema") != 1:
    raise SystemExit("发行清单格式错误")
if os.environ.get("AERO_VERSION") and manifest.get("version") != os.environ["AERO_VERSION"]:
    raise SystemExit("发行清单与指定版本不符，拒绝安装其他版本")
if os.environ.get("AERO_INSTALL_REVISION") and manifest.get("revision") != os.environ["AERO_INSTALL_REVISION"]:
    raise SystemExit("发行清单与指定测试修订不符")
profile = manifest.get("runtime", {"environment": "production"})
if profile.get("environment") != os.environ["AERO_ENVIRONMENT"]:
    raise SystemExit("发行清单所属环境不符，拒绝跨环境安装")
coordinator = json.loads(Path(sys.argv[3]).read_text())["manifest"]
if (coordinator.get("distribution") != "aerolytica" or coordinator.get("schema") != 1
    or coordinator.get("runtime", {"environment": "production"}).get("environment") != os.environ["AERO_ENVIRONMENT"]):
    raise SystemExit("安装引导所属环境或格式不正确")
for name in ("wheel", "bootstrap"):
    item = (coordinator if name == "bootstrap" else manifest)[name]
    path = item["path"]
    if not re.fullmatch(r"[A-Za-z0-9_./+\-]+", path) or path.startswith("/") or ".." in path.split("/"):
        raise SystemExit("发行路径不安全")
    if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
        raise SystemExit("发行校验值错误")
    (root / (name + ".path")).write_text(path)
    (root / (name + ".sha256")).write_text(item["sha256"] + "  " + name + "\n")
PY
for ITEM in wheel bootstrap; do
    ITEM_PATH="$(cat "$TEMP_DIR/$ITEM.path")"
    fetch "$AERO_DOWNLOAD_BASE/$ITEM_PATH" "$TEMP_DIR/$ITEM"
    check_hash "$ITEM"
done
printf '安装并验证客户端发行版…\n'
"$PYTHON" "$TEMP_DIR/bootstrap" --base "$AERO_DOWNLOAD_BASE" \
    --uv "$UV_DIR/uv" --manifest "$TEMP_DIR/stable.json" --wheel "$TEMP_DIR/wheel"
PROFILE_LINE="" # Keep post-install shell setup separate from version-selection smoke tests.
"$PYTHON" - <<'AERO_SHELL_PY'
# BEGIN AERO SHELL SETUP
import os
import re
import shlex
import stat
import tempfile
from pathlib import Path


def rc_files(shell, home, environ):
    override = environ.get("AERO_SHELL_RC_FILE")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError("--rc-file 必须为绝对路径")
        return [path]
    if shell == "zsh":
        return [Path(environ.get("ZDOTDIR") or home) / ".zshrc"]
    if shell == "bash":
        login = next(
            (
                home / name
                for name in (".bash_profile", ".bash_login", ".profile")
                if (home / name).is_file() and os.access(home / name, os.R_OK)
            ),
            home / ".bash_profile",
        )
        return [home / ".bashrc", login]
    if shell == "fish":
        return [Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "fish/config.fish"]
    if shell in {"sh", "dash", "ksh"}:
        return [home / ".profile"]
    return []


def quote(value, shell):
    if shell == "fish":
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return shlex.quote(value)


def path_block(directory, shell, environment):
    value = quote(str(directory), shell)
    if shell == "fish":
        body = f"if not contains -- {value} $PATH\n    set -gx PATH {value} $PATH\nend"
    else:
        body = (
            f'case ":$PATH:" in\n    *:{value}:*) ;;\n    *) export PATH={value}:"$PATH" ;;\nesac'
        )
    return (
        f"# >>> Aerolytica {environment} PATH >>>\n{body}\n# <<< Aerolytica {environment} PATH <<<"
    )


def update_rc(path, block, environment):
    target = path.resolve(strict=True) if path.is_symlink() else path
    if target.exists() and (not target.is_file() or target.stat().st_uid != os.getuid()):
        raise ValueError(f"配置不是当前用户拥有的普通文件：{path}")
    if target.exists() and not target.stat().st_mode & stat.S_IWUSR:
        raise ValueError(f"配置为只读文件，未修改：{path}")
    old = target.read_text() if target.exists() else ""
    start = f"# >>> Aerolytica {environment} PATH >>>"
    end = f"# <<< Aerolytica {environment} PATH <<<"
    if old.count(start) != old.count(end) or old.count(start) > 1:
        raise ValueError(f"配置标记不完整，请手动检查：{path}")
    if start in old and old.index(end) < old.index(start):
        raise ValueError(f"配置标记顺序错误，请手动检查：{path}")
    pattern = re.escape(start) + r".*?" + re.escape(end)
    new = (
        re.sub(pattern, lambda _: block, old, count=1, flags=re.S)
        if start in old
        else (old + ("\n" if old and not old.endswith("\n") else "") + "\n" + block + "\n")
    )
    if new == old:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if target.exists():
        fd, backup = tempfile.mkstemp(prefix=target.name + ".aerolytica-backup-", dir=target.parent)
        with os.fdopen(fd, "w") as handle:
            handle.write(old)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=".aerolytica-rc-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(new)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if (target.read_text() if target.exists() else "") != old:
            raise ValueError(f"配置在安装期间发生变化，未覆盖：{path}")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return backup


def main(environ=None):
    env = os.environ if environ is None else environ
    home = Path(env["HOME"])
    directory = Path(env["AERO_BIN_DIR"])
    command = "aero-testing" if env["AERO_ENVIRONMENT"] == "testing" else "aero"
    shell = env.get("AERO_INSTALL_SHELL", "")
    current_shell = env.get("AERO_CURRENT_SHELL", shell)
    mode = env.get("AERO_SHELL_INIT", "auto")
    tty = None
    path_configured = False
    if env.get("AERO_INSTALL_YES") != "1" and not (env.get("CI") or env.get("GITHUB_ACTIONS")):
        try:
            tty = open("/dev/tty", "r", buffering=1)
        except OSError:
            pass

    def ask(prompt, default):
        with open("/dev/tty", "w", buffering=1) as output:
            output.write(prompt)
        response = tty.readline()
        if not response:
            raise ValueError("已取消 shell 配置")
        return response.strip() or default

    try:
        if tty and mode != "no":
            shell = ask(
                f"\n检测到当前 shell：{current_shell or '不确定'}。选择要配置的 shell（zsh/bash/fish/sh）[{shell or '不确定'}]: ",
                shell,
            )
        if shell not in {"zsh", "bash", "fish", "sh", "dash", "ksh"}:
            print("未识别 shell，未修改任何配置；请使用完整路径：", directory / command)
            return
        paths = rc_files(shell, home, env)
        if any(not path.is_absolute() or "\n" in str(path) or "\r" in str(path) for path in paths):
            raise ValueError("shell 配置路径必须为不含换行的绝对路径；可用 --rc-file 指定")
        print("\n命令入口：", directory / command)
        for path in paths:
            print("建议配置：", str(path) + (f" → {path.resolve()}" if path.is_symlink() else ""))
        approved = mode == "yes"
        if tty and mode == "auto":
            approved = ask(
                "是否将命令目录写入 " + "、".join(map(str, paths)) + "？[Y/n] ", "y"
            ).lower() in {"y", "yes"}
        if approved:
            block = path_block(directory, shell, env["AERO_ENVIRONMENT"])
            for path in paths:
                backup = update_rc(path, block, env["AERO_ENVIRONMENT"])
                if backup:
                    print("原配置已备份：", backup)
            path_configured = True
            print(f"已配置。重新打开 {shell} 终端即可使用。")
            print("当前终端先执行：" if current_shell == shell else f"在 {shell} 中先执行：")
            print(
                "  source " + quote(str(paths[0]), shell)
                if shell in {"bash", "zsh", "fish"}
                else ". " + quote(str(paths[0]), shell)
            )
        else:
            print(f"未修改 shell 配置。可在 {shell} 中临时执行（不写入文件）：")
            line = (
                f"set -gx PATH {quote(str(directory), shell)} $PATH"
                if shell == "fish"
                else f'export PATH={quote(str(directory), shell)}:"$PATH"'
            )
            print("  " + line)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"客户端已安装，但 shell 配置未完成：{exc}")
        print("请使用完整路径：", directory / command)
    finally:
        if tty:
            tty.close()
        executable = command if path_configured else quote(str(directory / command), shell)
        if path_configured:
            print(f"\n完成上述配置加载，或重新打开 {shell} 终端后执行：")
        else:
            print("\n未配置 PATH，以下命令使用完整路径：")
        print(
            f"  {executable} setup\n\n进入研究目录后：\n  {executable} init\n  {executable} chat"
        )


if __name__ == "__main__":
    main()
# END AERO SHELL SETUP
AERO_SHELL_PY
printf '\n安装完成。\n'
